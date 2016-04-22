from __future__ import absolute_import, division, print_function

import atexit
import logging
import os
import signal
import time
from collections import deque

from mesos.interface import mesos_pb2
from mesos.native import MesosSchedulerDriver

from .interface import Scheduler
from .messages import PythonTaskStatus
from .proxies import SchedulerProxy
from .proxies.messages import FrameworkInfo, TaskInfo, encode


class PackError(Exception):
    pass


class Running(object):

    def __init__(self, scheduler, name, user='', master=os.getenv('MESOS_MASTER'),
                 implicit_acknowledge=1, *args, **kwargs):
        scheduler = SchedulerProxy(scheduler)
        framework = FrameworkInfo(name=name, user=user, *args, **kwargs)
        self.driver = MesosSchedulerDriver(scheduler, encode(framework),
                                           master, implicit_acknowledge)

        def shutdown(signal, frame):
            self.driver.stop()
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        atexit.register(self.driver.stop)

    def run(self):
        return self.driver.run()

    def start(self):
        status = self.driver.start()
        assert status == mesos_pb2.DRIVER_RUNNING
        return status

    def stop(self):
        logging.info("Stopping Mesos driver")
        self.driver.stop()
        logging.info("Joining Mesos driver")
        result = self.driver.join()
        logging.info("Joined Mesos driver")
        if result != mesos_pb2.DRIVER_STOPPED:
            raise RuntimeError("Mesos driver failed with %i", result)

    def join(self):
        return self.driver.join()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, type, value, traceback):
        self.stop()


class QueueScheduler(Scheduler):

    # TODO envargs
    def __init__(self, *args, **kwargs):
        self.queue = deque()  # holding unscheduled tasks
        self.running = {}  # holding task_id => task pairs

    def is_idle(self):
        return not len(self.queue) and not len(self.running)

    def wait(self):
        while not self.is_idle():
            time.sleep(0.1)

    def submit(self, task):  # supports commandtask, pythontask etc.
        assert isinstance(task, TaskInfo)
        self.queue.append(task)

    def on_offers(self, driver, offers):  # binpacking should be the default
        def pack(task, offers):
            for offer in offers:
                if offer >= task:
                    task.slave_id = offer.slave_id
                    return (offer, task)
            raise PackError('Couldn\'t pack')

        try:
            # should consider the whole queue as a list with a bin packing
            # solver
            task = self.queue.pop()
            offer, task = pack(task, offers)
        except PackError as e:
            # should reschedule if any error occurs at launch too
            self.tasks.append(task)
        except IndexError as e:
            # log empty queue
            pass
        else:
            offers.pop(offers.index(offer))
            driver.launch(offer.id, [task])
            self.running[task.id] = task
        finally:
            for offer in offers:
                driver.decline(offer.id)

    def on_update(self, driver, status):
        try:
            if status.is_terminated():
                task = self.running.pop(status.task_id)
            else:
                task = self.running[status.task_id]

            task.update(status)
        except:
            raise

        if len(self.queue) == 0 and len(self.running) == 0:
            driver.stop()


if __name__ == '__main__':
    scheduler = QueueScheduler()
    with Running(scheduler, name='test') as fw:
        scheduler.wait()
