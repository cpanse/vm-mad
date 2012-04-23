#! /usr/bin/env python
#
"""
Launch compute node VMs on demand.
"""
# Copyright (C) 2011, 2012 ETH Zurich and University of Zurich. All rights reserved.
#
# Authors:
#   Christian Panse <cp@fgcz.ethz.ch>
#   Riccardo Murri <riccardo.murri@gmail.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from __future__ import absolute_import

__docformat__ = 'reStructuredText'
__version__ = '$Revision$'


# stdlib imports
from abc import abstractmethod
import multiprocessing.dummy as mp
import os
import sys
import time

# local imports
from vmmad import log
from vmmad.util import random_password, Struct


class JobInfo(Struct):
    """
    Record data about a job in the batch system.

    A `JobInfo` object is basically a free-form record, but the
    constructor still enforces the following constraints:

    * There must be a `state` attribute, whose value is one of those listed below.
    * There must be a non-empty `jobid` attribute.

    The `state` attribute is set to one of the following strings
    (defaults to `DOWN` if not given in the constructor):

    ========= ============================================================
    state     meaning
    ========= ============================================================
    PENDING   The job is waiting in the batch system queue.
    RUNNING   The job is currently executing on some node.
    FINISHED  The job is done and any exec node it used is now clear
              for re-use by another job.
    OTHER     Unexpected/unhandled state; usually signals an error.
    ========= ============================================================

    Jobs in ``RUNNING`` state have a (string) `exec_node_name`
    attribute, which is used to match the associated VM (if any) by
    host name.

    """

    # job states
    PENDING = 1
    RUNNING = 2
    FINISHED = 3
    OTHER = 0

    def __init__(self, *args, **kwargs):
        Struct.__init__(self, *args, **kwargs)
        # ensure required fields are there
        assert 'jobid' in self, ("JobInfo object %s missing required field 'jobid'" % self)
        assert 'state' in self, ("JobInfo object %s missing required field 'state'" % self)
        assert self.state in [
            JobInfo.PENDING,
            JobInfo.RUNNING,
            JobInfo.FINISHED,
            JobInfo.OTHER
            ], \
            ("Invalid state '%s' for JobInfo object %s" % (self.state, self))


    def __str__(self):
        return ("Job %s" % self.jobid)
    

    def is_running(self):
        """
        Return `True` if the job is running.

        A running job is guaranteed to have a valid `exec_node_name`
        attribute.
        """
        if self.state == JobInfo.RUNNING:
            assert 'exec_node_name' in self, \
                   ("JobInfo object %s marked RUNNING"
                    " but missing required field 'exec_node_name'"
                    % (self,))
            return True
        else:
            return False


class VmInfo(Struct):
    """
    Record data about a started VM instance.

    A `VmInfo` object is mostly a free-form record, but the
    constructor still enforces the following constraints:

    * There must be a non-empty `vmid` attribute.

    .. note::

      The `vmid` attribute must be unique among all `VmInfo` instances!

      **FIXME:** This is not currently enforced by the constructor.

    The `state` attribute is set to one of the following strings
    (defaults to `DOWN` if not given in the constructor):

    ========= ============================================================
    status    meaning
    ========= ============================================================
    STARTING  A request to start the VM has been sent to the Cloud provider,
              but the VM is not ready yet.
    UP        The machine is up and running, and connected to the network.
    READY     The machine is ready to run jobs.
    STOPPING  The remote VM has been frozen, but normal execution can be resumed.
    DOWN      The VM has been stopped and cannot be restarted/resumed.
    OTHER     Unexpected/unhandled state; usually signals an error.
    ========= ============================================================


    The following attributes are available after the machine has
    reached the ``UP`` state:

    ============  ========  ================================================
    attribute     type      meaning
    ============  ========  ================================================
    public_ip     str       public IP address in dot quad notation
    private_ip    str       private IP address in dot quad notation
    cloud         str       cloud identifier
    started_at    datetime  when the request to start the machine was issued
    ready_at      datetime  when the machine was first detected up and running
    running_time  duration  total running time (in seconds)
    bill          float     total cost to be billed by cloud provider (in US$)
    ============  ========  ================================================


    The following attributes are available after the machine has
    reached the ``READY`` state:

    ============  ========  ================================================
    attribute     type      meaning
    ============  ========  ================================================
    ready_at      datetime  when the machine notified it's ready to run jobs
    jobs          list      list of Job IDs of jobs running on this node
    nodename      str       machine node name (as reported in the batch system listing)
    ============  ========  ================================================
    """

    STARTING = 'STARTING'
    UP = 'UP'
    READY = 'READY'
    STOPPING = 'STOPPING'
    DOWN = 'DOWN'
    OTHER = 'OTHER'

    def __init__(self, *args, **kwargs):
        Struct.__init__(self, *args, **kwargs)
        # ensure required fields are there
        assert 'vmid' in self, ("VmInfo object %s missing required field 'vmid'" % self)
        # provide defaults
        if 'state' not in self:
            self.state = VmInfo.DOWN
        else:
            assert self.state in [
                VmInfo.STARTING,
                VmInfo.UP,
                VmInfo.STOPPING,
                VmInfo.DOWN,
                VmInfo.OTHER
                ]
        if 'bill' not in self:
            self.bill = 0.0
        if 'jobs' not in self:
            self.jobs = set()


    def __str__(self):
        if 'nodename' in self:
            return ("VM Node '%s'" % self.nodename)
        else:
            return ("VM %s" % self.vmid)
    __repr__ = __str__

    
    def __hash__(self):
        """Use the VM id as unique hash value."""
        return hash(self.vmid)


    def is_alive(self):
        """
        Return `True` if the VM is up or will soon be (i.e., it is starting now).
        """
        return self.state in [VmInfo.STARTING, VmInfo.UP]


## the main class of this file

class Orchestrator(object):
    """
    The `Orchestrator` monitors a job queue and starts/stops VMs
    according to a pre-defined policy in order to offload jobs from
    the queue.

    `Orchestrator` is an abstract class that cannot be directly
    instanciated: subclassing is needed to define a policy and
    implement interfaces to an actual queuing system and cloud
    backend.

    The actual VM start/stop policy is defined by overriding methods
    `is_new_vm_needed` and `can_vm_be_stopped`.  Each of these methods
    can access a list of candidate jobs as attribute
    `self.candidates`; the `can_vm_be_stopped` method is additionally
    passed a `VmInfo` object and can inspect data from that VM.

    The `cloud` argument must be an object that implements the interface
    defined by the abstract class `vmmad.cloud.Cloud`.

    The `batchsys` argument must be an object that implements the
    interface defined by the abstract class
    `vmmad.batchsys.BatchSystem`:class: (which see for details).

    The `threads` argument specifies the size of the thread pool that
    is used to perform blocking operations.
    """

    def __init__(self, cloud, batchsys, max_vms, max_delta=1, threads=8):
        # thread pool to enqueue blocking operations
        self._par = mp.Pool(threads)

        # allocator for shared memory objects
        self._shm = mp.Manager()
        
        # cloud provider
        self.cloud = cloud

        # batch system interface
        self.batchsys = batchsys
        
        # max number of VMs that are allocated on the cloud
        self.max_vms = max_vms

        # max number of VMs that can be started each cycle
        self.max_delta = max_delta
        
        # VMs controlled by this `Orchestrator` instance (indexed by VMID)
        self._started_vms = self._shm.dict()
        self._stopping_vms = self._shm.dict()
        self._active_vms = { }
        self._waiting_for_auth = { }
        
        # mapping jobid to job informations
        self.jobs = [ ]
        self.candidates = { }

        # VM book-keeping
        self._vmid = 0
        
        # Time simulation variable
        self.cycle = 0

        # Time the job statuses were last checked
        self.last_update = 0


    def run(self, delay=30, max_cycles=0):
        """
        Run the orchestrator main loop until stopped or `max_cycles` reached.

        Every `delay` seconds, the following operations are performed
        in sequence:

        - update job and VM status;
        - start new VMs if needed;
        - stop running VMs if they are no longer needed.
        """
        done = 0
        last_cycle_at = self.time()
        while max_cycles == 0 or done < max_cycles:
            log.debug("Orchestrator %x about to start cycle %d", id(self), self.cycle)
            now = self.time()
            t0 = time.time() # need real time, not the simulated one
            
            self.before()
            self.update_job_status()
            # XXX: potentially blocking - should timeout!
            self.cloud.update_vm_status(self._started_vms.values())
            for vm in self._started_vms.values():
                if not vm.jobs:
                    vm.total_idle += (now - last_cycle_at)
                    vm.last_idle += (now - last_cycle_at)
                else:
                    vm.last_idle = 0

            # start new VMs if needed
            if self.is_new_vm_needed() and len(self._started_vms) < self.max_vms:
                # new VMID
                self._vmid += 1
                # generate a random auth token and ensure it's not in use
                passwd = random_password()
                while passwd in self._waiting_for_auth:
                    passwd = random_password()
                # bundle up all this into a VM object
                new_vm = VmInfo(
                    vmid=str(self._vmid),
                    state=VmInfo.STARTING,
                    auth=passwd,
                    total_idle=0,
                    last_idle=0)
                # start it!
                self._waiting_for_auth[passwd] = vm
                self._par.apply_async(self._asynch_start_vm, (new_vm,))

            # stop VMs that are no longer needed
            # XXX: We move the VmInfo object to a separate list so
            # that it's no longer considered "started" and examined in
            # this main loop: if the stopping process takes a long
            # time, we might end up trying to stop the same instance
            # twice concurrently, which we do not want or need.
            # FIXME: The trick of modifying `self._started_vms` as we
            # iterate on it only works because (in Python 2.x), the
            # `.items()` method returns a *copy* of the items list; in
            # Python 3.x, where it returns an iterator, this construct
            # will *not* work!
            for vmid, vm in self._started_vms.items():
                if self.can_vm_be_stopped(vm):
                    if len(vm.jobs) > 0:
                        log.warning(
                            "Request to stop VM %s, but it's still running jobs: %s",
                            vm.vmid, str.join(' ', vm.jobs))
                    self._stopping_vms[vmid] = vm
                    if vm.state == VmInfo.READY:
                        del self._active_vms[vm.nodename]
                    del self._started_vms[vmid]
                    self._par.apply_async(self._asynch_stop_vm, (vm,))

            self.after()
            self.cycle +=1
            done += 1
            last_cycle_at = now
            
            if delay > 0:
                t1 = time.time() # need real time, not the simulated one
                elapsed = t1 - t0
                if elapsed > delay:
                    log.warning("Cycle %d took more than %.2f seconds!"
                                " Starting new cycle without delay."
                                % (self.cycle, delay))
                else:
                    time.sleep(delay - elapsed)

    def _asynch_start_vm(self, vm):
        assert vm.vmid not in self._started_vms
        log.info("Starting VM %s ...", vm.vmid)
        try:
            self.cloud.start_vm(vm)
            vm.started_at = self.time()
            self._started_vms[vm.vmid] = vm
            log.info("VM %s started, waiting for 'READY' notification.", vm.vmid)
        except Exception, ex:
            vm.state = VmInfo.DOWN
            log.error("Error starting VM %s: %s: %s",
                      vm.vmid, ex.__class__.__name__, str(ex), exc_info=__debug__)
            if vm.vmid in self._started_vms:
                del _started_vms[vm.vmid]

    def _asynch_stop_vm(self, vm):
        log.info("Stopping VM %s ...", vm.vmid)
        try:
            self.cloud.stop_vm(vm)
            vm.stopped_at = self.time()
            del self._stopping_vms[vm.vmid]
            been_running = (vm.stopped_at - vm.ready_at)
            log.info("Stopped VM %s (%s);"
                     " it has run for %d seconds, been idle for %d of them (%.2f%%)",
                     vm.vmid, vm.nodename, been_running, vm.total_idle,
                     (100.0 * vm.total_idle / been_running))
            vm.state = VmInfo.DOWN
        except Exception, ex:
            # XXX: This is more delicate than catching errors in the
            # startup phase: if a VM was not stopped when it should
            # have been, we run the chance of being billed from the
            # cloud provider for services we do not use any longer.
            # What's the correct course of action?
            log.error("Error stopping VM %s: %s: %s",
                      vm.vmid, ex.__class__.__name__, str(ex), exc_info=__debug__)
            

    def before(self):
        """Hook called at the start of the main run() cycle."""
        pass

    def after(self):
        """Hook called at the end of the main run() cycle."""
        pass


    def time(self):
        """
        Return the current time in UNIX epoch format.

        This method exists only for the purpose of overriding it
        insimulator classes, so that a 'virtual' time can be
        implemented.
        """
        return time.time()


    def update_job_status(self):
        """
        Update job information based on what the batch system interface returns.

        Return the full list of active job objects (i.e., not just the
        candidates for cloud execution).
        """
        now = self.time()
        jobs = self.batchsys.get_sched_info()    
        
        # remove finished jobs
        jobids = set(job.jobid for job in jobs)
        for vm in self._active_vms.itervalues():
            # remove jobs that are no longer in the list, i.e., they are finished
            terminated = (vm.jobs - jobids)
            for jobid in terminated:
                log.info("Job %s terminated its execution on node '%s'",
                         jobid, vm.nodename)
            vm.jobs -= terminated

        # update info on running jobs
        for job in jobs:
            if job.state == JobInfo.RUNNING and job.running_at > self.last_update:
                log.info("Job %s was started on node '%s'", job.jobid, job.exec_node_name)
                # job just went running, it's longer a candidate
                if job.jobid in self.candidates:
                    del self.candidates[job.jobid]
                # record which jobs are running on which VM
                if job.exec_node_name in self._active_vms:
                    self._active_vms[job.exec_node_name].jobs.add(job.jobid)
            elif job.state == JobInfo.PENDING and job.submitted_at > self.last_update:
                # update candidates' information
                if self.is_cloud_candidate(job):
                    self.candidates[job.jobid] = job
            else:
                # ignore
                pass

        self.last_update = now
        return jobs
    

    def vm_is_ready(self, auth, nodename):
        """
        Notify an `Orchestrator` instance that a VM is ready to accept jobs.

        The `auth` argument must match one of the passwords assigned
        to a previously-started VM (which are passed to the VM via
        boot parameters).  If it does not, an error is logged and the
        call immediately returns `False`.

        The `nodename` argument must be the host name that the VM
        reports to the batch system, i.e., the node name as it appears
        in the batch system scheduler listings/config. 
        """
        if auth not in self._waiting_for_auth:
            log.error(
                "Received notification that node '%s' is READY,"
                " but authentication data does not match any started VM.  Ignoring.",
                nodename)
            return False
        vm = self._waiting_for_auth.pop(auth)
        assert vm.state in [ VmInfo.STARTING, VmInfo.UP ]
        vm.state = VmInfo.READY
        vm.ready_at = self.time()
        vm.nodename = nodename
        self._active_vms[nodename] = vm
        log.info("VM %s reports being ready as node '%s'", vm.vmid, nodename)
        return True


    ##
    ## policy implementation interface
    ##
    @abstractmethod
    def is_cloud_candidate(self, job):
        """Return `True` if `job` can be run in a cloud node.

        Override in subclasses to define a different cloud-burst
        policy.
        """
        return False


    def is_new_vm_needed(self):
        """Inspect job collection and decide whether we need to start new VMs."""
        if len(self.candidates) > 0:
            return True


    @abstractmethod
    def can_vm_be_stopped(self, vm):
        """Return `True` if the VM identified by `vm` is no longer
        needed and can be stopped.
        """
        pass



## main: run tests


if "__main__" == __name__:
    # XXX: won't work, as Orchestrator is an abstract class
    Orchestrator().run()
