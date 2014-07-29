import linecache
import os.path
import re
import traceback  # @Reimport

from django_debug import find_django_render_frame
from django_debug import is_django_render_call, is_django_suspended, suspend_django, is_django_resolve_call, is_django_context_get_call
from django_frame import DjangoTemplateFrame
from django_frame import is_django_exception_break_context
from django_frame import just_raised, get_template_file_name, get_template_line
import pydev_log
from pydevd_breakpoints import get_exception_breakpoint, get_exception_name
from pydevd_comm import CMD_ADD_DJANGO_EXCEPTION_BREAK, \
    CMD_STEP_CAUGHT_EXCEPTION, CMD_STEP_RETURN, CMD_STEP_OVER, CMD_SET_BREAK, \
    CMD_STEP_INTO, CMD_SMART_STEP_INTO, CMD_RUN_TO_LINE, CMD_SET_NEXT_STATEMENT
from pydevd_constants import *  # @UnusedWildImport
from pydevd_file_utils import GetFilenameAndBase
from pydevd_signature import sendSignatureCallTrace
import pydevd_vars

basename = os.path.basename

IGNORE_EXCEPTION_TAG = re.compile('[^#]*#.*@IgnoreException')


#=======================================================================================================================
# PyDBFrame
#=======================================================================================================================
class PyDBFrame:
    '''This makes the tracing for a given frame, so, the trace_dispatch
    is used initially when we enter into a new context ('call') and then
    is reused for the entire context.
    '''

    #Note: class (and not instance) attributes.

    #Same thing in the main debugger but only considering the file contents, while the one in the main debugger
    #considers the user input (so, the actual result must be a join of both).
    filename_to_lines_where_exceptions_are_ignored = {}
    filename_to_stat_info = {}

    def __init__(self, args):
        #args = mainDebugger, filename, base, info, t, frame
        #yeap, much faster than putting in self and then getting it from self later on
        self._args = args[:-1]

    def setSuspend(self, *args, **kwargs):
        self._args[0].setSuspend(*args, **kwargs)

    def doWaitSuspend(self, *args, **kwargs):
        self._args[0].doWaitSuspend(*args, **kwargs)

    def trace_exception(self, frame, event, arg):
        if event == 'exception':
            flag, frame = self.shouldStopOnException(frame, event, arg)

            if flag:
                self.handle_exception(frame, event, arg)
                return self.trace_dispatch

        return self.trace_exception

    def shouldStopOnException(self, frame, event, arg):
        mainDebugger, _filename, info, thread = self._args
        flag = False

        if info.pydev_state != STATE_SUSPEND:  #and breakpoint is not None:
            exception, value, trace = arg

            if trace is not None: #on jython trace is None on the first event
                exception_breakpoint = get_exception_breakpoint(
                    exception, mainDebugger.break_on_caught_exceptions.copy())

                if exception_breakpoint is not None:
                    if not exception_breakpoint.notify_on_first_raise_only or just_raised(trace):
                        # print frame.f_code.co_name
                        add_exception_to_frame(frame, (exception, value, trace))
                        thread.additionalInfo.message = exception_breakpoint.qname
                        flag = True
                    else:
                        flag = False
                else:
                    try:
                        if mainDebugger.django_exception_break and get_exception_name(exception) in [
                                'VariableDoesNotExist', 'TemplateDoesNotExist', 'TemplateSyntaxError'] \
                                and just_raised(trace) and is_django_exception_break_context(frame):

                            render_frame = find_django_render_frame(frame)
                            if render_frame:
                                suspend_frame = suspend_django(
                                    self, mainDebugger, thread, render_frame, CMD_ADD_DJANGO_EXCEPTION_BREAK)

                                if suspend_frame:
                                    add_exception_to_frame(suspend_frame, (exception, value, trace))
                                    flag = True
                                    thread.additionalInfo.message = 'VariableDoesNotExist'
                                    suspend_frame.f_back = frame
                                    frame = suspend_frame
                    except :
                        flag = False

        return flag, frame

    def handle_exception(self, frame, event, arg):
        try:
            # print 'handle_exception', frame.f_lineno, frame.f_code.co_name

            # We have 3 things in arg: exception type, description, traceback object
            trace_obj = arg[2]
            mainDebugger = self._args[0]

            if not hasattr(trace_obj, 'tb_next'):
                return  #Not always there on Jython...

            initial_trace_obj = trace_obj
            if trace_obj.tb_next is None and trace_obj.tb_frame is frame:
                #I.e.: tb_next should be only None in the context it was thrown (trace_obj.tb_frame is frame is just a double check).

                if mainDebugger.break_on_exceptions_thrown_in_same_context:
                    #Option: Don't break if an exception is caught in the same function from which it is thrown
                    return
            else:
                #Get the trace_obj from where the exception was raised...
                while trace_obj.tb_next is not None:
                    trace_obj = trace_obj.tb_next


            if mainDebugger.ignore_exceptions_thrown_in_lines_with_ignore_exception:
                for check_trace_obj in (initial_trace_obj, trace_obj):
                    filename = GetFilenameAndBase(check_trace_obj.tb_frame)[0]


                    filename_to_lines_where_exceptions_are_ignored = self.filename_to_lines_where_exceptions_are_ignored


                    lines_ignored = filename_to_lines_where_exceptions_are_ignored.get(filename)
                    if lines_ignored is None:
                        lines_ignored = filename_to_lines_where_exceptions_are_ignored[filename] = {}

                    try:
                        curr_stat = os.stat(filename)
                        curr_stat = (curr_stat.st_size, curr_stat.st_mtime)
                    except:
                        curr_stat = None

                    last_stat = self.filename_to_stat_info.get(filename)
                    if last_stat != curr_stat:
                        self.filename_to_stat_info[filename] = curr_stat
                        lines_ignored.clear()
                        try:
                            linecache.checkcache(filename)
                        except:
                            #Jython 2.1
                            linecache.checkcache()

                    from_user_input = mainDebugger.filename_to_lines_where_exceptions_are_ignored.get(filename)
                    if from_user_input:
                        merged = {}
                        merged.update(lines_ignored)
                        #Override what we have with the related entries that the user entered
                        merged.update(from_user_input)
                    else:
                        merged = lines_ignored

                    exc_lineno = check_trace_obj.tb_lineno

                    # print ('lines ignored', lines_ignored)
                    # print ('user input', from_user_input)
                    # print ('merged', merged, 'curr', exc_lineno)

                    if not DictContains(merged, exc_lineno):  #Note: check on merged but update lines_ignored.
                        try:
                            line = linecache.getline(filename, exc_lineno, check_trace_obj.tb_frame.f_globals)
                        except:
                            #Jython 2.1
                            line = linecache.getline(filename, exc_lineno)

                        if IGNORE_EXCEPTION_TAG.match(line) is not None:
                            lines_ignored[exc_lineno] = 1
                            return
                        else:
                            #Put in the cache saying not to ignore
                            lines_ignored[exc_lineno] = 0
                    else:
                        #Ok, dict has it already cached, so, let's check it...
                        if merged.get(exc_lineno, 0):
                            return


            thread = self._args[3]

            try:
                frame_id_to_frame = {}
                f = trace_obj.tb_frame
                while f is not None:
                    frame_id_to_frame[id(f)] = f
                    f = f.f_back
                f = None

                thread_id = GetThreadId(thread)
                pydevd_vars.addAdditionalFrameById(thread_id, frame_id_to_frame)
                try:
                    mainDebugger.sendCaughtExceptionStack(thread, arg, id(frame))
                    self.setSuspend(thread, CMD_STEP_CAUGHT_EXCEPTION)
                    self.doWaitSuspend(thread, frame, event, arg)
                    mainDebugger.sendCaughtExceptionStackProceeded(thread)

                finally:
                    pydevd_vars.removeAdditionalFrameById(thread_id)
            except:
                traceback.print_exc()

            mainDebugger.SetTraceForFrameAndParents(frame)
        finally:
            #Clear some local variables...
            trace_obj = None
            initial_trace_obj = None
            check_trace_obj = None
            f = None
            frame_id_to_frame = None
            mainDebugger = None
            thread = None

    def trace_dispatch(self, frame, event, arg):
        mainDebugger, filename, info, thread = self._args
        try:
            info.is_tracing = True

            if mainDebugger._finishDebuggingSession:
                return None

            if getattr(thread, 'pydev_do_not_trace', None):
                return None

            if event == 'call':
                sendSignatureCallTrace(mainDebugger, frame, filename)

            is_exception_event = event == 'exception'

            if event not in ('line', 'call', 'return'):
                if is_exception_event:
                    flag, frame = self.shouldStopOnException(frame, event, arg)
                    if flag:
                        self.handle_exception(frame, event, arg)
                        return self.trace_dispatch
                else:
                    #I believe this can only happen in jython on some frontiers on jython and java code, which we don't want to trace.
                    return None

            stop_frame = info.pydev_step_stop
            step_cmd = info.pydev_step_cmd

            if is_exception_event:
                breakpoints_for_file = None
            else:
                breakpoints_for_file = mainDebugger.breakpoints.get(filename)

                can_skip = False

                if info.pydev_state == STATE_RUN:
                    #we can skip if:
                    #- we have no stop marked
                    #- we should make a step return/step over and we're not in the current frame
                    can_skip = (step_cmd is None and stop_frame is None)\
                        or (step_cmd in (CMD_STEP_RETURN, CMD_STEP_OVER) and stop_frame is not frame)

                if mainDebugger.django_breakpoints:
                    can_skip = False

                # Let's check to see if we are in a function that has a breakpoint. If we don't have a breakpoint,
                # we will return nothing for the next trace
                #also, after we hit a breakpoint and go to some other debugging state, we have to force the set trace anyway,
                #so, that's why the additional checks are there.
                if not breakpoints_for_file:
                    if can_skip:
                        if mainDebugger.break_on_caught_exceptions or mainDebugger.django_exception_break:
                            return self.trace_exception
                        else:
                            return None

                else:
                    #checks the breakpoint to see if there is a context match in some function
                    curr_func_name = frame.f_code.co_name

                    #global context is set with an empty name
                    if curr_func_name in ('?', '<module>'):
                        curr_func_name = ''

                    for breakpoint in breakpoints_for_file.values(): #jython does not support itervalues()
                        #will match either global or some function
                        if breakpoint.func_name in ('None', curr_func_name):
                            break

                    else: # if we had some break, it won't get here (so, that's a context that we want to skip)
                        if can_skip:
                            #print 'skipping', frame.f_lineno, info.pydev_state, stop_frame, step_cmd
                            return None

            #We may have hit a breakpoint or we are already in step mode. Either way, let's check what we should do in this frame
            #print 'NOT skipped', frame.f_lineno, frame.f_code.co_name, event

            try:
                line = frame.f_lineno


                flag = False
                if event == 'call' and info.pydev_state != STATE_SUSPEND and mainDebugger.django_breakpoints \
                    and is_django_render_call(frame):
                    (flag, frame) = self.shouldStopOnDjangoBreak(frame, event, arg)

                #return is not taken into account for breakpoint hit because we'd have a double-hit in this case
                #(one for the line and the other for the return).

                if not flag and event != 'return' and info.pydev_state != STATE_SUSPEND and breakpoints_for_file is not None\
                    and DictContains(breakpoints_for_file, line):
                    #ok, hit breakpoint, now, we have to discover if it is a conditional breakpoint
                    # lets do the conditional stuff here
                    breakpoint = breakpoints_for_file[line]

                    stop = True
                    if step_cmd == CMD_STEP_OVER and stop_frame is frame and event in ('line', 'return'):
                        stop = False #we don't stop on breakpoint if we have to stop by step-over (it will be processed later)
                    else:
                        if breakpoint.condition is not None:
                            try:
                                val = eval(breakpoint.condition, frame.f_globals, frame.f_locals)
                                if not val:
                                    return self.trace_dispatch

                            except:
                                pydev_log.info('Error while evaluating condition \'%s\': %s\n' % (breakpoint.condition, sys.exc_info()[1]))

                                return self.trace_dispatch

                    if breakpoint.expression is not None:
                        try:
                            try:
                                val = eval(breakpoint.expression, frame.f_globals, frame.f_locals)
                            except:
                                val = sys.exc_info()[1]
                        finally:
                            if val is not None:
                                thread.additionalInfo.message = val

                    if stop:
                        self.setSuspend(thread, CMD_SET_BREAK)

                # if thread has a suspend flag, we suspend with a busy wait
                if info.pydev_state == STATE_SUSPEND:
                    self.doWaitSuspend(thread, frame, event, arg)
                    return self.trace_dispatch

            except:
                traceback.print_exc()
                raise

            #step handling. We stop when we hit the right frame
            try:
                django_stop = False
                if step_cmd == CMD_STEP_INTO:
                    stop = event in ('line', 'return')
                    if is_django_suspended(thread):
                        #django_stop = event == 'call' and is_django_render_call(frame)
                        stop = stop and is_django_resolve_call(frame.f_back) and not is_django_context_get_call(frame)
                        if stop:
                            info.pydev_django_resolve_frame = 1 #we remember that we've go into python code from django rendering frame

                elif step_cmd == CMD_STEP_OVER:
                    if is_django_suspended(thread):
                        django_stop = event == 'call' and is_django_render_call(frame)

                        stop = False
                    else:
                        if event == 'return' and info.pydev_django_resolve_frame is not None and is_django_resolve_call(frame.f_back):
                            #we return to Django suspend mode and should not stop before django rendering frame
                            stop_frame = info.pydev_step_stop = info.pydev_django_resolve_frame
                            info.pydev_django_resolve_frame = None
                            thread.additionalInfo.suspend_type = DJANGO_SUSPEND


                        stop = stop_frame is frame and event in ('line', 'return')

                elif step_cmd == CMD_SMART_STEP_INTO:
                    stop = False
                    if info.pydev_smart_step_stop is frame:
                        info.pydev_func_name = None
                        info.pydev_smart_step_stop = None

                    if event == 'line' or event == 'exception':
                        curr_func_name = frame.f_code.co_name

                        #global context is set with an empty name
                        if curr_func_name in ('?', '<module>') or curr_func_name is None:
                            curr_func_name = ''

                        if curr_func_name == info.pydev_func_name:
                            stop = True

                elif step_cmd == CMD_STEP_RETURN:
                    stop = event == 'return' and stop_frame is frame

                elif step_cmd == CMD_RUN_TO_LINE or step_cmd == CMD_SET_NEXT_STATEMENT:
                    stop = False

                    if event == 'line' or event == 'exception':
                        #Yes, we can only act on line events (weird hum?)
                        #Note: This code is duplicated at pydevd.py
                        #Acting on exception events after debugger breaks with exception
                        curr_func_name = frame.f_code.co_name

                        #global context is set with an empty name
                        if curr_func_name in ('?', '<module>'):
                            curr_func_name = ''

                        if curr_func_name == info.pydev_func_name:
                            line = info.pydev_next_line
                            if frame.f_lineno == line:
                                stop = True
                            else:
                                if frame.f_trace is None:
                                    frame.f_trace = self.trace_dispatch
                                frame.f_lineno = line
                                frame.f_trace = None
                                stop = True

                else:
                    stop = False

                if django_stop:
                    frame = suspend_django(self, mainDebugger, thread, frame)
                    if frame:
                        self.doWaitSuspend(thread, frame, event, arg)
                elif stop:
                    #event is always == line or return at this point
                    if event == 'line':
                        self.setSuspend(thread, step_cmd)
                        self.doWaitSuspend(thread, frame, event, arg)
                    else: #return event
                        back = frame.f_back
                        if back is not None:
                            #When we get to the pydevd run function, the debugging has actually finished for the main thread
                            #(note that it can still go on for other threads, but for this one, we just make it finish)
                            #So, just setting it to None should be OK
                            if basename(back.f_code.co_filename) == 'pydevd.py' and back.f_code.co_name == 'run':
                                back = None

                        if back is not None:
                            #if we're in a return, we want it to appear to the user in the previous frame!
                            self.setSuspend(thread, step_cmd)
                            self.doWaitSuspend(thread, back, event, arg)
                        else:
                            #in jython we may not have a back frame
                            info.pydev_step_stop = None
                            info.pydev_step_cmd = None
                            info.pydev_state = STATE_RUN


            except:
                traceback.print_exc()
                info.pydev_step_cmd = None

            #if we are quitting, let's stop the tracing
            retVal = None
            if not mainDebugger.quitting:
                retVal = self.trace_dispatch

            return retVal
        finally:
            info.is_tracing = False

        #end trace_dispatch

    if USE_PSYCO_OPTIMIZATION:
        try:
            import psyco

            trace_dispatch = psyco.proxy(trace_dispatch)
        except ImportError:
            if hasattr(sys, 'exc_clear'): #jython does not have it
                sys.exc_clear() #don't keep the traceback
            pass #ok, psyco not available

    def shouldStopOnDjangoBreak(self, frame, event, arg):
        mainDebugger = self._args[0]
        thread = self._args[3]
        flag = False
        template_frame_file = get_template_file_name(frame)

        #pydev_log.debug("Django is rendering a template: %s\n" % template_frame_file)

        django_breakpoints_for_file = mainDebugger.django_breakpoints.get(template_frame_file)
        if django_breakpoints_for_file:

            #pydev_log.debug("Breakpoints for that file: %s\n" % django_breakpoints_for_file)

            template_frame_line = get_template_line(frame, template_frame_file)

            #pydev_log.debug("Tracing template line: %d\n" % template_frame_line)

            if DictContains(django_breakpoints_for_file, template_frame_line):
                django_breakpoint = django_breakpoints_for_file[template_frame_line]

                if django_breakpoint.is_triggered(template_frame_file, template_frame_line):

                    #pydev_log.debug("Breakpoint is triggered.\n")

                    flag = True
                    new_frame = DjangoTemplateFrame(
                        frame,
                        template_frame_file=template_frame_file,
                        template_frame_line=template_frame_line,
                    )

                    if django_breakpoint.condition is not None:
                        try:
                            val = eval(django_breakpoint.condition, new_frame.f_globals, new_frame.f_locals)
                            if not val:
                                flag = False
                                pydev_log.debug("Condition '%s' is evaluated to %s. Not suspending.\n" % (django_breakpoint.condition, val))
                        except:
                            pydev_log.info(
                                'Error while evaluating condition \'%s\': %s\n' % (django_breakpoint.condition, sys.exc_info()[1]))

                    if django_breakpoint.expression is not None:
                        try:
                            try:
                                val = eval(django_breakpoint.expression, new_frame.f_globals, new_frame.f_locals)
                            except:
                                val = sys.exc_info()[1]
                        finally:
                            if val is not None:
                                thread.additionalInfo.message = val
                    if flag:
                        frame = suspend_django(self, mainDebugger, thread, frame)
        return flag, frame

def add_exception_to_frame(frame, exception_info):
    frame.f_locals['__exception__'] = exception_info