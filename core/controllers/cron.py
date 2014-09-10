# Copyright 2014 The Oppia Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Controllers for the cron jobs."""

import ast
import logging

from core import jobs
from core.controllers import base
from core.platform import models
email_services = models.Registry.import_email_services()
(job_models,) = models.Registry.import_models([models.NAMES.job])
import feconf
import utils

from mapreduce import model as mapreduce_model
from mapreduce.lib.pipeline import pipeline

# The default retention timeline is 2 days.
MAX_MAPREDUCE_METADATA_RETENTION_MSECS = 2 * 24 * 60 * 60 * 1000
# Name of an additional parameter to pass into the MR job for cleaning up
# old auxiliary job models.
MAPPER_PARAM_MAX_START_TIME_MSEC = 'max_start_time_msec'


def require_cron_or_superadmin(handler):
    """Decorator to ensure that the handler is being called by cron or by a
    superadmin of the application.
    """
    def _require_cron_or_superadmin(self, *args, **kwargs):
        if (self.request.headers.get('X-AppEngine-Cron') is None
                and not self.is_super_admin):
            raise self.UnauthorizedUserException(
                'You do not have the credentials to access this page.')
        else:
            return handler(self, *args, **kwargs)

    return _require_cron_or_superadmin


class JobStatusMailerHandler(base.BaseHandler):
    """Handler for mailing admin about job failures."""

    @require_cron_or_superadmin
    def get(self):
        """Handles GET requests."""
        TWENTY_FIVE_HOURS_IN_MSECS = 25 * 60 * 60 * 1000
        MAX_JOBS_TO_REPORT_ON = 50

        # TODO(sll): Get the 50 most recent failed shards, not all of them.
        failed_jobs = jobs.get_stuck_jobs(TWENTY_FIVE_HOURS_IN_MSECS)
        if failed_jobs:
            email_subject = 'MapReduce failure alert'
            email_message = (
                '%s jobs have failed in the past 25 hours. More information '
                '(about at most %s jobs; to see more, please check the logs):'
            ) % (len(failed_jobs), MAX_JOBS_TO_REPORT_ON)

            for job in failed_jobs[:MAX_JOBS_TO_REPORT_ON]:
                email_message += '\n'
                email_message += '-----------------------------------'
                email_message += '\n'
                email_message += (
                    'Job with mapreduce ID %s (key name %s) failed. '
                    'More info:\n\n'
                    '  counters_map: %s\n'
                    '  shard_retries: %s\n'
                    '  slice_retries: %s\n'
                    '  last_update_time: %s\n'
                    '  last_work_item: %s\n'
                ) % (
                    job.mapreduce_id, job.key().name(), job.counters_map,
                    job.retries, job.slice_retries, job.update_time,
                    job.last_work_item
                )
        else:
            email_subject = 'MapReduce status report'
            email_message = 'All MapReduce jobs are running fine.'

        email_services.send_mail_to_admin(
            feconf.ADMIN_EMAIL_ADDRESS, email_subject, email_message)


class _JobCleanupManager(jobs.BaseMapReduceJobManager):
    """One-off job for cleaning up old auxiliary entities for MR jobs."""

    @classmethod
    def entity_classes_to_map_over(cls):
        return [
            mapreduce_model.MapreduceState,
            mapreduce_model.ShardState
        ]

    @staticmethod
    def map(item):
        max_start_time_msec = _JobCleanupManager.get_mapper_param(
            MAPPER_PARAM_MAX_START_TIME_MSEC)

        if isinstance(item, mapreduce_model.MapreduceState):
            if (item.result_status == 'success' and
                    utils.get_time_in_millisecs(item.start_time) <
                    max_start_time_msec):
                item.delete()
                yield ('mr_state_deleted', 1)
            else:
                yield ('mr_state_remaining', 1)

        if isinstance(item, mapreduce_model.ShardState):
            if (item.result_status == 'success' and
                    utils.get_time_in_millisecs(item.update_time) <
                    max_start_time_msec):
                item.delete()
                yield ('shard_state_deleted', 1)
            else:
                yield ('mr_state_remaining', 1)

    @staticmethod
    def reduce(key, stringified_values):
        values = [ast.literal_eval(v) for v in stringified_values]
        if key.endswith('_deleted'):
            logging.warning(
                'Delete count: %s entities (%s)' % (sum(values), key))
        else:
            logging.warning(
                'Entities remaining count: %s entities (%s)' %
                (sum(values), key))


class CronMapreduceCleanupHandler(base.BaseHandler):

    def get(self):
        """Clean up intermediate data items for completed or failed M/R jobs.

        Map/reduce runs leave around a large number of rows in several
        tables.  This data is useful to have around for a while:
        - it helps diagnose any problems with jobs that may be occurring
        - it shows where resource usage is occurring
        However, after a few days, this information is less relevant, and
        should be cleaned up.
        """
        self._clean_mapreduce(MAX_MAPREDUCE_METADATA_RETENTION_MSECS)

    @classmethod
    def _clean_mapreduce(cls, recency_msec):
        """Cleans up all MR jobs that started more than recency_msec
        milliseconds ago.
        """
        num_cleaned = 0

        min_age_msec = recency_msec
        # Only consider jobs that started at most 1 week before recency_msec.
        max_age_msec = recency_msec + 7 * 24 * 60 * 60 * 1000
        # The latest start time that a job scheduled for cleanup may have.
        max_start_time_msec = (
            utils.get_current_time_in_millisecs() - min_age_msec)

        # Get all pipeline ids from jobs that started between max_age_msecs
        # and max_age_msecs + 1 week, before now.
        pipeline_id_to_job_instance = {}

        job_instances = job_models.JobModel.get_recent_jobs(1000, max_age_msec)
        for job_instance in job_instances:
            if (job_instance.time_started_msec < max_start_time_msec and not
                    job_instance.has_been_cleaned_up):
                if 'root_pipeline_id' in job_instance.metadata:
                    pipeline_id = job_instance.metadata['root_pipeline_id']
                    pipeline_id_to_job_instance[pipeline_id] = job_instance

        # Clean up pipelines.
        for pline in pipeline.get_root_list()['pipelines']:
            pipeline_id = pline['pipelineId']
            job_definitely_terminated = (
                pline['status'] == 'done' or
                pline['status'] == 'aborted' or
                pline['currentAttempt'] > pline['maxAttempts'])
            have_start_time = 'startTimeMs' in pline
            job_started_too_long_ago = (
                have_start_time and
                pline['startTimeMs'] < max_start_time_msec)

            if (job_started_too_long_ago or
                (not have_start_time and job_definitely_terminated)):
                # At this point, the map/reduce pipeline is either in a
                # terminal state, or has taken so long that there's no
                # realistic possibility that there might be a race condition
                # between this and the job actually completing.
                if pipeline_id in pipeline_id_to_job_instance:
                    job_instance = pipeline_id_to_job_instance[pipeline_id]
                    job_instance.has_been_cleaned_up = True
                    job_instance.put()

                # This enqueues a deferred cleanup item.
                p = pipeline.Pipeline.from_id(pipeline_id)
                if p:
                    p.cleanup()
                    num_cleaned += 1

        logging.warning('%s MR jobs cleaned up.' % num_cleaned)

        if job_models.JobModel.do_unfinished_jobs_exist('_JobCleanupManager'):
            logging.warning('A previous cleanup job is still running.')
        else:
            _JobCleanupManager.enqueue(
                _JobCleanupManager.create_new(), additional_job_params={
                    MAPPER_PARAM_MAX_START_TIME_MSEC: max_start_time_msec
                })
            logging.warning('Deletion jobs for auxiliary entities kicked off.')
