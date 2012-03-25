#  Copyright 2011 Tijmen Roberti
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""
This file contains all taskqueue handler for work that can be
performed in the background. Tasks in the taskqueue sense are called
'workers', to prevent any confusing with Tasks in the SPS sense.
"""
import os
import logging
from google.appengine.api import users, datastore
from google.appengine.api import taskqueue
from google.appengine.ext import db
from google.appengine.datastore import datastore_rpc
import webapp2 as webapp
import json
import api
from model import Domain, Task, TaskIndex, Context, User

# A test to check if we are on the development sdk, as that one
# does not support multi entity groups yet.
import os
DEV_SERVER = os.environ.get('SERVER_SOFTWARE','').startswith('Development')


class UpdateTaskCompletion(webapp.RequestHandler):
    """
    Updates all derived properties of the tasks in a hierarchy.
    This worker starts from a given task and then propagates upwards
    all the derived properties that depend on properties lower in the
    hierarchy (ie. those properties whose initial values are set as
    atomic tasks).

    To update the properties that start from a root task, use the
    other worker.

    This post requests takes two arguments, a domain and a task
    identifier. The update touches approximately b * d tasks, where
    b is the average branching factor in the task hierarchy and
    d the depth of the starting task in the hierarchy.

    This operation is idempotent.
    """
    def post(self):
        domain_identifier = self.request.get('domain')
        domain_key = Domain.key_from_name(domain_identifier)
        task_identifier = self.request.get('task')

        def txn():
            task = api.get_task(domain_identifier, task_identifier)
            if not task:
                logging.error("Task '%s/%s' does not exist",
                              domain_identifier, task_identifier)
                return
            index = TaskIndex.get_by_key_name(task_identifier, parent=task)
            if not index:
                index = TaskIndex(parent=task, key_name=task_identifier)
            # Get all subtasks. The ancestor queries are strongly
            # consistent, so when propagating upwards through the
            # hierarchy the changes are reflected.
            subtasks = list(Task.all().
                            ancestor(domain_key).
                            filter('parent_task =', task.key()))
            if not subtasks:    # atomic task
                task.derived_completed = task.completed
                task.derived_size = 1
                task.derived_atomic_task_count = 1
                task.derived_has_open_tasks = task.open()
                assignee_identifier = task.assignee_identifier()
                if assignee_identifier:
                    index.assignees = [assignee_identifier]
                    if not DEV_SERVER:
                        # Uses a multi entity group transaction to get the name
                        # of the assignee. This is cached in the record for
                        # quick descriptions.
                        assignee = api.get_user(assignee_identifier)
                        name = assignee.name if assignee else '<Missing>'
                    else:
                        name = 'temp'
                    task.derived_assignees[task.assignee_identifier()] = {
                        'id': task.assignee_identifier(),
                        'name': name,
                        'completed': int(task.is_completed()),
                        'all': 1
                        }
            else:               # composite task
                task.derived_completed = all(t.is_completed() for t in subtasks)
                task.derived_size = 1 + sum(t.derived_size for t in subtasks)
                task.derived_atomic_task_count = sum(t.atomic_task_count()
                                                     for t in subtasks)
                task.derived_has_open_tasks = any(t.has_open_tasks()
                                                  for t in subtasks)
                # Compute derived assignees, and sum the total of all
                # their assigned and completed subtasks.
                assignees = {}
                for subtask in subtasks:
                    subtask_assignees = subtask.derived_assignees
                    for id, record in subtask_assignees.iteritems():
                        if not id in assignees:
                            assignees[id] = {
                                'id': id,
                                'name': record['name'],
                                'completed': 0,
                                'all': 0
                                }
                        assignees[id]['completed'] += record['completed']
                        assignees[id]['all'] += record['all']
                task.derived_assignees = assignees
                index.assignees = list(assignees.iterkeys())
            task.put()
            index.completed = task.is_completed()
            index.has_open_tasks = task.has_open_tasks()
            index.atomic = task.atomic()
            index.put()
            # Propagate further upwards
            if task.parent_task_identifier():
                UpdateTaskCompletion.enqueue(domain_identifier,
                                             task.parent_task_identifier(),
                                             transactional=True)
        # Use a multi entity group transaction, if available
        if DEV_SERVER:
            db.run_in_transaction(txn)
        else:
            xg_on = db.create_transaction_options(xg=True)
            db.run_in_transaction_options(xg_on, txn)



    @staticmethod
    def enqueue(domain_identifier, task_identifier, transactional=False):
        """
        Queues a new worker to update the task hierarchy of the task
        with the given identifier.

        Args:
            domain_identifier: The domain identifier string
            task_identifier: The task identifier string
            transactional: If set to true, then the task will be added
                as a transactional task.

        Raises:
            ValueError: If transactional is set to True and the
                 function is not called as part of a transaction.
        """
        if transactional and not db.is_in_transaction():
            raise ValueError("Adding a transactional worker requires a"
                             " transaction")

        queue = taskqueue.Queue('update-task-hierarchy')
        task = taskqueue.Task(url='/workers/update-task-completion',
                              params={ 'task': task_identifier,
                                       'domain': domain_identifier })
        try:
            queue.add(task, transactional=transactional)
        except taskqueue.TransientError:
            queue.add(task, transactional=transactional)



class UpdateTaskHierarchy(webapp.RequestHandler):
    """
    Updates the task level and hierarchy fields of a task hierarchy.
    The update starts at the given task, and propagates all the way
    downwards in the entire tree.

    This post request takes two arguments, a domain and a task identifier.
    The update touches all the tasks in the hierarchy.

    This operation is idempotent.
    """
    def post(self):
        domain_identifier = self.request.get('domain')
        domain_key = Domain.key_from_name(domain_identifier)
        task_identifier = self.request.get('task')

        def txn():
            task = api.get_task(domain_identifier, task_identifier)
            if not task:
                logging.error("Task '%s/%s' does not exist",
                              domain_identifier, task_identifier)
                return None
            parent_task = api.get_task(domain_identifier,
                                       task.parent_task_identifier())
            if parent_task:
                parent_index = TaskIndex.get_by_key_name(
                    parent_task.identifier(),
                    parent=parent_task.key())
                if not parent_index:
                    logging.error("Missing index for parent task '%s/%s'",
                                  domain_identifier, parent_identifier)
                    self.error(400) # Retry later
                    return None
                hierarchy = list(parent_index.hierarchy)
                hierarchy.append(parent_task.identifier())
                level = parent_task.derived_level + 1
            else:               # root task
                hierarchy = []
                level = 0
            index = TaskIndex.get_by_key_name(task_identifier, parent=task)
            if not index:
                index = TaskIndex(parent=task, key_name=task_identifier)
            index.hierarchy = hierarchy
            index.put()
            task.derived_level = level
            task.put()
            return task

        task = db.run_in_transaction(txn)
        if not task:
            return

        # Spawn new tasks to propagate downwards. This is done outside
        # the transaction, as only 5 transactional tasks can be
        # queued. It is not a problem if the tasks will fail after the
        # transaction, as this task is then retried, so the
        # propagation will always proceeed.
        query = Task.all(keys_only=True).\
            ancestor(Domain.key_from_name(domain_identifier)).\
            filter('parent_task =', task.key())
        for subtask_key in query:
            subtask_identifier = subtask_key.id_or_name()
            # TODO(tijmen): Batch queue tasks
            UpdateTaskHierarchy.enqueue(domain_identifier, subtask_identifier)

    @staticmethod
    def enqueue(domain_identifier, task_identifier, transactional=False):
        """
        Queues a new worker to update the task hierarchy of the task
        with the given identifier.

        Args:
            domain_identifier: The domain identifier string
            task_identifier: The task identifier string
            transactional: If set to true, then the task will be added
                as a transactional task.

        Raises:
            ValueError: If transactional is set to True and the
                 function is not called as part of a transaction.
        """
        if transactional and not db.is_in_transaction():
            raise ValueError("Adding a transactional worker requires a"
                             " transaction")

        queue = taskqueue.Queue('update-task-hierarchy')
        task = taskqueue.Task(url='/workers/update-task-hierarchy',
                              params={ 'task': task_identifier,
                                       'domain': domain_identifier })
        try:
            queue.add(task, transactional=transactional)
        except taskqueue.TransientError:
            queue.add(task, transactional=transactional)


mapping = [
    ('/workers/update-task-hierarchy', UpdateTaskHierarchy),
    ('/workers/update-task-completion', UpdateTaskCompletion)
    ]

application = webapp.WSGIApplication(mapping)
