# -*- coding: utf-8 -*-
# kate: replace-tabs off; indent-width 4; indent-mode normal
# vim: ts=4:sw=4:noexpandtab

# strongly inspired from http://www.dabeaz.com/coroutines/

from collections import deque
import time
import heapq
import copy
import inspect

# ------------------------------------------------------------
#                       === Tasks ===
# ------------------------------------------------------------
class Task(object):
	""" The object representing a task/co-routine in the scheduler """
	WAIT_ANY = 1
	WAIT_ALL = 2
	taskid = 0
	def __init__(self,target):
		""" Initialize """
		Task.taskid += 1
		self.tid     = Task.taskid   # Task ID
		self.target  = target        # Target coroutine
		self.sendval = None          # Value to send
		self.waitmode = Task.WAIT_ANY
	def __repr__(self):
		""" Debug information on a task """
		return 'Task ' + str(self.tid) + ' (' + self.target.__name__ + ') @ ' + str(id(self))
	def run(self):
		""" Run a task until it hits the next yield statement"""
		return self.target.send(self.sendval)

# ------------------------------------------------------------
#                === Conditional Variables ===
# ------------------------------------------------------------
class ConditionVariable(object):
	""" The basic conditional variable """
	def __init__(self, initval=None):
		""" Initialize """
		self.val = initval
		self.myname = None
	def __get__(self, obj, objtype):
		""" Return the value """
		return self.val
	def __set__(self, obj, val):
		""" Set a value, evaluate conditions for tasks waiting on this variable """
		self.val = val
		self._set_name(type(obj))
		obj.test_conditions(self.myname)
	def _set_name(self, cls, top_level=True):
		""" if unknown, retrieve my own name using introspection """
		if self.myname is None:
			members = cls.__dict__
			# first look into members
			for name, value in members.iteritems():
				if value is self:
					self.myname = name
					break
			# look into parents
			for parent_cls in cls.__bases__:
				self._set_name(parent_cls,False)
			# if not found and top-level, assert
			if top_level:
				assert self.myname is not None

# ------------------------------------------------------------
#                      === Scheduler ===
# ------------------------------------------------------------
class Scheduler(object):
	""" The scheduler base object, do not instanciate directly """
	def __init__(self):
		# Map of all tasks
		self.taskmap = {}
		# Deque of ready tasks
		self.ready   = deque()   
		# Tasks waiting for other tasks to exit, map of: tid => list of tasks
		self.exit_waiting = {}
		# Task waiting on conditions, map of: "name of condition variable" => (condition, task)
		self.cond_waiting = {}
		# Task being paused by another task
		self.paused_in_syscall = set()
		self.paused_in_ready = set()
	
	def new(self,target):
		newtask = Task(target)
		self.taskmap[newtask.tid] = newtask
		self.schedule(newtask)
		self.log_task_created(newtask)
		return newtask.tid
	
	# temporary test for kill_task
	def kill_task(self,tid):
		task = self.taskmap.get(tid,None)
		if task:
			task.target.close() 
			return True
		else:
			return False

	def exit(self,exiting_task):
		self.log_task_terminated(exiting_task)
		del self.taskmap[exiting_task.tid]
		# Notify other tasks waiting for exit
		to_remove_keys = []
		for task in self.exit_waiting.pop(exiting_task.tid,[]):
			if task.waitmode == Task.WAIT_ANY:
				# remove associations to other tasks waited on
				for waited_tid, waiting_tasks_list in self.exit_waiting.iteritems():
					# remove task form list of waiting tasks if in there
					for waiting_task in waiting_tasks_list:
						if waiting_task.tid == task.tid:
							waiting_tasks_list.remove(waiting_task)
				# return the tid of the exiting_task 
				task.sendval = exiting_task.tid
				self.schedule(task)
			else:
				are_still_waiting = False
				for waited_tid, waiting_tasks_list in self.exit_waiting.iteritems():
					for waiting_task in waiting_tasks_list:
						if waiting_task.tid == task.tid:
							are_still_waiting = True
				if not are_still_waiting:
					# return the tid of the exiting_task 
					task.sendval = exiting_task.tid
					self.schedule(task)
		self.exit_waiting = dict((k,v) for (k,v) in self.exit_waiting.iteritems() if v)

	def wait_for_exit(self,task,waittid):
		if waittid in self.taskmap:
			self.exit_waiting.setdefault(waittid,[]).append(task)
			return True
		else:
			return False

	def schedule(self,task):
		if task in self.paused_in_syscall:
			self.paused_in_syscall.remove(task)
			self.paused_in_ready.add(task)
		else:
			self.ready.append(task)
	
	def schedule_now(self,task):
		if task in self.paused_in_syscall:
			self.paused_in_syscall.remove(task)
			self.paused_in_ready.add(task)
		else:
			self.ready.appendleft(task)
	
	def pause_task(self,task):
		if task is None or task in self.paused_in_ready or task in self.paused_in_syscall:
			return False
		if task in self.ready:
			self.ready.remove(task)
			self.paused_in_ready.add(task)
		else:
			self.paused_in_syscall.add(task)
		return True
	
	def resume_task(self,task):
		if task is None:
			return False
		if task in self.paused_in_ready:
			# execute the resumed task directly once we exit the syscall
			self.paused_in_ready.remove(task)
			self.ready.appendleft(task)
			return True
		elif task in self.paused_in_syscall:
			self.paused_in_syscall.remove(task)
			return True
		return False
		
	def wait_duration(self,task,duration):
		def resume(task):
			self.schedule_now(task)
		self.set_timer_callback(self.current_time()+duration, lambda: resume(task))
	
	def wait_duration_rate(self,task,duration,rate):
		def resume(task,rate):
			# get current time
			rate.last_time = self.current_time()
			# if not paused, execute the resumed task directly once we exit the syscall
			self.schedule_now(task)
		self.set_timer_callback(self.current_time()+duration, lambda: resume(task, rate))
	
	def _add_condition(self,entry):
		condition = entry[0]
		vars_in_cond = dict(inspect.getmembers(dict(inspect.getmembers(condition))['func_code']))['co_names']
		for var in vars_in_cond:
			if var not in self.cond_waiting:
				self.cond_waiting[var] = []
			self.cond_waiting[var].append(entry)
	
	def _del_condition(self,candidate):
		(condition, task) = candidate
		vars_in_cond = dict(inspect.getmembers(dict(inspect.getmembers(condition))['func_code']))['co_names']
		for var in vars_in_cond:
			if var in self.cond_waiting:
				self.cond_waiting[var].remove(candidate)
				if not self.cond_waiting[var]:
					del self.cond_waiting[var]
	
	def wait_condition(self,task,condition):
		# add a new condition and directly evalutate it once
		entry = (condition,task)
		if not condition():
			self._add_condition(entry)
		else:
			self.schedule_now(task)
		
	def test_conditions(self, name):
		# is there any task waiting on this name?
		if name not in self.cond_waiting:
			return
		# check which conditions are true
		candidates = copy.copy(self.cond_waiting[name])
		for candidate in candidates:
			(condition, task) = candidate
			if task not in self.paused_in_syscall and condition():
				self.schedule(task)
				self._del_condition(candidate)
	
	def step(self):
		""" Run all tasks until none is ready """
		#print 'ready queue A: ' + str(self.ready)
		while self.ready:
			task = self.ready.popleft()
			try:
				#print 'Running ' + str(task)
				result = task.run()
				if isinstance(result,SystemCall):
					result.task  = task
					result.sched = self
					result.handle()
					#print 'ready queue B: ' + str(self.ready)
					continue
			except StopIteration:
				self.exit(task)
				continue
			self.schedule(task)
	
	# Methods that might or must be overridden by children
	
	def current_time(self):
		""" Get current time """
		return time.time()
	
	def sleep(self, duration):
		""" Sleep a certain amount of time """
		time.sleep(duration)
	
	def set_timer_callback(self, t, f):
		""" Execute function f at time t """
		raise NotImplementedError('timer callback mechanism must be provided by derived class')
	
	def log_task_created(self, task):
		""" Log for task created """
		print time.ctime() + " - Task %s (tid %d) created" % (task.target.__name__, task.tid)
	
	def log_task_terminated(self, task):
		""" Log for task terminated """
		print time.ctime() + " - Task %s (tid %d) terminated" % (task.target.__name__, task.tid)

class TimerScheduler(Scheduler):
	""" A scheduler that sleeps when there is nothing to do. """
	
	def __init__(self):
		""" Initialize """
		super(TimerScheduler, self).__init__()
		self.timer_cb = []
		self.timer_counter = 0
	
	def set_timer_callback(self, t, f):
		""" Implement the timer callback """
		heapq.heappush(self.timer_cb, [t, self.timer_counter, f])
		self.timer_counter += 1
	
	def run(self):
		""" Run until there is no task to schedule """
		while self.timer_cb or self.ready or self.cond_waiting:
			self.step()
			t, counter, f = heapq.heappop(self.timer_cb)
			duration = t - self.current_time()
			if duration >= 0:
				self.sleep(duration)
			f()
			self.step()
	
	def timer_step(self):
		""" Schedule all tasks with past deadlines and step """
		while self.timer_cb:
			t, counter, f = heapq.heappop(self.timer_cb)
			duration = t - self.current_time()
			if duration <= 0:
				f()
			else:
				heapq.heappush(self.timer_cb, [t, counter, f])
				break
		self.step()
	
# ------------------------------------------------------------
#                   === Helper objects ===
# ------------------------------------------------------------

class Rate(object):
	""" Helper class to execute a loop at a certain rate """
	def __init__(self,duration,initial_time):
		""" Initialize """
		self.duration = duration
		self.last_time = initial_time
	def sleep(self,sched,task):
		""" Sleep for the rest of this period """
		cur_time = sched.current_time()
		delta_time = self.duration - (cur_time - self.last_time)
		if delta_time > 0:
			sched.wait_duration_rate(task, delta_time, self)
		else:
			sched.schedule(task)
		return delta_time

# ------------------------------------------------------------
#                   === System Calls ===
# ------------------------------------------------------------

class SystemCall(object):
	""" Parent of all system calls """
	def handle(self):
		""" Called in the scheduler context """
		pass

class GetScheduler(SystemCall):
	""" Return the scheduler, useful to access condition variables """
	def handle(self):
		self.task.sendval = self.sched
		self.sched.schedule(self.task)

class GetTid(SystemCall):
	""" Return a task's ID number """
	def handle(self):
		self.task.sendval = self.task.tid
		self.sched.schedule(self.task)

class GetTids(SystemCall):
	""" Return all task IDs """
	def handle(self):
		self.task.sendval = self.sched.taskmap.keys()
		self.sched.schedule(self.task)

class NewTask(SystemCall):
	""" Create a new task, return the task identifier """
	def __init__(self,target):
		self.target = target
	def handle(self):
		tid = self.sched.new(self.target)
		self.task.sendval = tid
		self.sched.schedule(self.task)

class KillTask(SystemCall):
	""" Kill a task, return whether the task was killed """
	def __init__(self,tid):
		self.tid = tid
	def handle(self):
		task = self.sched.taskmap.get(self.tid,None)
		if task:
			task.target.close() 
			self.task.sendval = True
		else:
			self.task.sendval = False
		self.sched.schedule(self.task)

class KillTasks(SystemCall):
	""" Kill multiple tasks, return the list of killed tasks """
	def __init__(self,tids):
		self.tids = tids
	def handle(self):
		self.task.sendval = []
		for tid in self.tids:
			task = self.sched.taskmap.get(tid,None)
			if task:
				task.target.close() 
				self.task.sendval.append(tid)
		self.sched.schedule(self.task)

class KillAllTasksExcept(SystemCall):
	""" Kill all tasks except a subset, return the list of killed tasks """
	def __init__(self,except_tids):
		self.except_tids = except_tids
	def handle(self):
		self.task.sendval = []
		for tid, task in self.sched.taskmap.items():
			if tid not in self.except_tids:
				task.target.close()
				self.task.sendval.append(task)
		self.sched.schedule(self.task)

class WaitTask(SystemCall):
	""" Wait for a task to exit, return whether the wait was a success """
	def __init__(self,tid):
		self.tid = tid
	def handle(self):
		result = self.sched.wait_for_exit(self.task,self.tid)
		self.task.sendval = result
		self.task.waitmode = Task.WAIT_ANY
		# If waiting for a non-existent task,
		# return immediately without waiting
		if not result:
			self.sched.schedule(self.task)


class WaitAnyTasks(SystemCall):
	""" Wait for any tasks to exit, return whether the wait was a success """
	def __init__(self,tids):
		self.tids = tids
	def handle(self):
		self.task.waitmode = Task.WAIT_ANY
		# Check if all tasks exist
		all_exist = True
		non_existing_tid = None
		for tid in self.tids:
			if tid not in self.sched.taskmap:
				all_exist = False
				non_existing_tid = tid
				break
		# If all exist
		if all_exist:
			for tid in self.tids:
				self.sched.wait_for_exit(self.task,tid)
			#dont set sendval, we want exit() to assign the exiting tasks tid
			#self.task.sendval = True
		else:
			# If waiting for a non-existent task,
			# return immediately without waiting
			self.task.sendval = non_existing_tid
			self.sched.schedule(self.task)

class WaitAllTasks(SystemCall):
	""" Wait for all tasks to exit, return whether the wait was a success """
	def __init__(self,tids):
		self.tids = tids
	def handle(self):
		self.task.waitmode = Task.WAIT_ALL
		any_exist = False
		for tid in self.tids:
			result = self.sched.wait_for_exit(self.task,tid)
			any_exist = any_exist or result
		# If waiting for non-existent tasks,
		# return immediately without waiting
		if any_exist:
			self.task.sendval = True			
		else:
			self.task.sendval = False
			self.sched.schedule(self.task)

class PauseTask(SystemCall):
	""" Pause a task, return whether the task was paused successfully """
	def __init__(self,tid):
		self.tid = tid
	def handle(self):
		task = self.sched.taskmap.get(self.tid,None)
		self.task.sendval = self.sched.pause_task(task)
		self.sched.schedule(self.task)

class PauseTasks(SystemCall):
	""" Pause multiple tasks, return the list of paused tasks """
	def __init__(self,tids):
		self.tids = tids
	def handle(self):
		self.task.sendval = []
		for tid in self.tids:
			task = self.sched.taskmap.get(tid,None)
			if self.sched.pause_task(task):
				self.task.sendval.append(tid)
		self.sched.schedule(self.task)

class ResumeTask(SystemCall):
	""" Resume a task, return whether the task was resumed successfully """
	def __init__(self,tid):
		self.tid = tid
	def handle(self):
		task = self.sched.taskmap.get(self.tid,None)
		self.task.sendval = self.sched.resume_task(task)
		self.sched.schedule(self.task)

class ResumeTasks(SystemCall):
	""" Resume the execution of given tasks, return the list of resumed tasks """
	def __init__(self,tids):
		self.tids = tids
	def handle(self):
		self.task.sendval = []
		for tid in self.tids:
			task = self.sched.taskmap.get(tid,None)
			if self.sched.resume_task(task):
				self.task.sendval.append(tid)
		self.sched.schedule(self.task)

class GetCurrentTime(SystemCall):
	""" Return the current time """
	def handle(self):
		self.task.sendval = self.sched.current_time()
		self.sched.schedule(self.task)

class WaitDuration(SystemCall):
	""" Pause current task for a certain duration """
	def __init__(self,duration):
		self.duration = duration
	def handle(self):
		self.sched.wait_duration(self.task, self.duration)
		self.task.sendval = None

class WaitCondition(SystemCall):
	""" Pause current task until the condition is true """
	def __init__(self,condition):
		self.condition = condition
	def handle(self):
		self.sched.wait_condition(self.task,self.condition)
		self.task.sendval = None

class CreateRate(SystemCall):
	""" Create a rate object, to have loops of certain frequencies """
	def __init__(self,rate):
		self.duration = 1./rate
	def handle(self):
		initial_time = self.sched.current_time()
		self.task.sendval = Rate(self.duration, initial_time)
		self.sched.schedule(self.task)

class Sleep(SystemCall):
	""" Sleep using a rate object """
	def __init__(self,rate):
		self.rate = rate
	def handle(self):
		self.task.sendval = self.rate.sleep(self.sched, self.task)
		
class TeerPrint(SystemCall):
	""" Print something including the current task id"""
	def __init__(self, msg):
		self.msg = msg
	def handle(self):
		print "[teer tid: " + str(self.task.tid) + "] " + self.msg
		self.sched.schedule(self.task)
