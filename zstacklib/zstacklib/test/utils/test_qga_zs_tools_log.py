import sys,types,unittest,os
try:
 from unittest import mock
except ImportError:
 class FakeCall(object):
  def __init__(self,args,kwargs):self.args=args;self.kwargs=kwargs
  def __getitem__(self,index):return (self.args,self.kwargs)[index]
 class FakeMock(object):
  def __init__(self,return_value=None,side_effect=None):
   self.return_value=return_value;self.side_effect=side_effect;self.reset_mock()
  def reset_mock(self):self.call_args_list=[];self.call_count=0
  def assert_not_called(self):assert self.call_count==0
  def __call__(self,*args,**kwargs):
   self.call_args_list.append(FakeCall(args,kwargs));self.call_count+=1
   effect=self.side_effect
   if isinstance(effect,BaseException):raise effect
   if callable(effect):return effect(*args,**kwargs)
   if effect is not None:
    result=effect.pop(0)
    if isinstance(result,BaseException):raise result
    return result
   return self.return_value
 class mock(object):Mock=FakeMock
source=os.environ.get('QGA_TEST_SOURCE',os.path.join(
 os.path.dirname(__file__),'..','..','utils','qga.py'))
previous=dict((name,sys.modules.get(name)) for name in ('libvirt','libvirt_qemu','log'))
try:
 for name in previous:sys.modules[name]=types.ModuleType(name)
 sys.modules['log'].get_logger=lambda name:None
 sys.modules['log'].mask_sensitive_field=lambda obj,value:'<masked>'
 ns={};exec(compile(open(source,'rb').read(),source,'exec'),ns)
finally:
 for name,module in previous.items():
  if module is None:sys.modules.pop(name,None)
  else:sys.modules[name]=module
class FakeClock(object):
 def __init__(self):self.elapsed=0;self.sleeps=[]
 def time(self):return self.elapsed
 def sleep(self,seconds):self.sleeps.append(seconds);self.elapsed+=seconds

class TailTest(unittest.TestCase):
 def setUp(self):
  self.vm=ns['VmQga'].__new__(ns['VmQga']);self.vm.vm_uuid='test-vm'
  self.vm.call_qga_command=mock.Mock()
  ns.get('_zs_tools_net_pending',{}).clear()
  self.clock=FakeClock();self.original_time=ns['time'];ns['time']=self.clock
 def tearDown(self):
  ns.get('_zs_tools_net_pending',{}).clear()
  ns['time']=self.original_time
 def sequence(self,size=9000,data=b'last\r\nlines'):
  self.vm.call_qga_command.side_effect=[7,{'position':size},{},{'buf-b64':data},{}]
 def test_current_package_path(self):
  self.sequence();self.assertEqual('last\nlines',self.vm._try_read_zs_tools_log_tail())
  self.assertEqual(r'C:\Program Files\GuestTools\zs-tools\zs-tools.log',self.vm.call_qga_command.call_args_list[0][0][1]['path'])
 def test_executable_relative_path(self):
  self.vm.ZS_TOOLS_PATN_WIN=r'D:\Tools\zs-tools.exe';self.sequence();self.vm._try_read_zs_tools_log_tail()
  self.assertEqual(r'D:\Tools\zs-tools.log',self.vm.call_qga_command.call_args_list[0][0][1]['path'])
 def test_legacy_paths(self):
  for count in (1,2):
   self.vm.call_qga_command.reset_mock()
   self.vm.call_qga_command.side_effect=[RuntimeError('file not found')]*count+[7,{'position':2},{},{'buf-b64':b'hi'},{}]
   self.assertEqual('hi',self.vm._try_read_zs_tools_log_tail())
   self.assertIn(r'\log\zs-tools.log',self.vm.call_qga_command.call_args_list[count][0][1]['path'])
 def test_exact_tail_size(self):
  self.sequence(data=b'x'*4096);self.assertEqual(4096,len(self.vm._try_read_zs_tools_log_tail(999999)))
  calls=self.vm.call_qga_command.call_args_list
  self.assertEqual(4904,calls[2][0][1]['offset']);self.assertEqual(4096,calls[3][0][1]['count'])
  self.assertTrue(all(call[1]['timeout']==1 for call in calls))
 def test_short_and_empty(self):
  for size in (0,2):
   self.sequence(size=size,data=b'hi'[:size]);self.assertEqual('hi'[:size],self.vm._try_read_zs_tools_log_tail())
   self.assertEqual(0,self.vm.call_qga_command.call_args_list[-3][0][1]['offset'])
 def test_disabled_read(self):
  self.assertEqual('',self.vm._try_read_zs_tools_log_tail(0));self.vm.call_qga_command.assert_not_called()
 def test_sharing_diagnostic_and_redaction(self):
  self.vm.call_qga_command.side_effect=RuntimeError('sharing violation password=secret')
  detail=self.vm._get_zs_tools_log_diagnostic()
  self.assertIn('tail unavailable',detail);self.assertIn('sharing violation',detail);self.assertNotIn('secret',detail)
  self.assertEqual(15,self.vm.call_qga_command.call_count);self.assertLess(len(detail),1000)
  self.assertEqual(3,self.clock.elapsed);self.assertIn("5 attempt(s)",detail)
 def test_release_during_retry(self):
  self.vm.call_qga_command.side_effect=[RuntimeError('sharing violation'),RuntimeError('sharing violation'),7,{'position':3},{},{'buf-b64':b'ok!'},{}]
  self.assertEqual('ok!',self.vm._try_read_zs_tools_log_tail())
  self.assertEqual([.25,.25],self.clock.sleeps)
  self.assertEqual([],self.vm._zs_tools_log_tail_failures)
  opens=[c for c in self.vm.call_qga_command.call_args_list if c[0][0]=='guest-file-open']
  self.assertEqual(3,len(opens));self.assertEqual(opens[0][0][1]['path'],opens[-1][0][1]['path'])
 def test_nonsharing_errors_are_not_retried(self):
  for reason in ('file not found','access denied','QGA timed out'):
   self.vm.call_qga_command.reset_mock();self.vm.call_qga_command.side_effect=RuntimeError(reason)
   self.vm._try_read_zs_tools_log_tail()
   self.assertEqual(3,self.vm.call_qga_command.call_count)
  self.assertEqual([],self.clock.sleeps)
 def test_total_time_budget(self):
  def slow_open(*args,**kwargs):
   self.clock.elapsed+=1
   raise RuntimeError('sharing violation')
  self.vm.call_qga_command.side_effect=slow_open
  self.assertEqual('',self.vm._try_read_zs_tools_log_tail())
  self.assertLessEqual(self.clock.elapsed,5)
  self.assertEqual(4,self.vm.call_qga_command.call_count)
  self.assertIn('sharing violation',' '.join(self.vm._zs_tools_log_tail_failures))
 def test_budget_still_closes_handle(self):
  def slow_call(command,args,**kwargs):
   self.clock.elapsed+=1
   if command=='guest-file-open':return 7
   if command=='guest-file-seek':return {'position':6}
   if command=='guest-file-read':raise RuntimeError('sharing violation')
   return {}
  self.vm.call_qga_command.side_effect=slow_call
  self.assertEqual('',self.vm._try_read_zs_tools_log_tail())
  self.assertEqual('guest-file-close',self.vm.call_qga_command.call_args_list[-1][0][0])
  self.assertLessEqual(self.clock.elapsed,6)
 def test_caller_deadline_prevents_io_without_close_reserve(self):
  self.assertEqual('',self.vm._try_read_zs_tools_log_tail(deadline=2.5))
  self.vm.call_qga_command.assert_not_called()
  self.assertIn('QGA timeout',' '.join(self.vm._zs_tools_log_tail_failures))
 def test_caller_deadline_reserves_close(self):
  def slow_call(command,args,**kwargs):
   self.clock.elapsed+=1
   return 7 if command=='guest-file-open' else {}
  self.vm.call_qga_command.side_effect=slow_call
  self.assertEqual('',self.vm._try_read_zs_tools_log_tail(deadline=3))
  self.assertEqual(['guest-file-open','guest-file-close'],[c[0][0] for c in self.vm.call_qga_command.call_args_list])
  self.assertLessEqual(self.clock.elapsed,3)
 def setup_net(self,phase='finished',exitcode=2):
  self.vm._net_task_launch_args=mock.Mock(return_value={})
  self.vm.guest_exec=mock.Mock(return_value={'pid':77})
  self.vm.guest_exec_status_with_timeout=mock.Mock(return_value={'exited':True})
  self.vm._recover_net_task=mock.Mock()
  self.vm._net_task_control=mock.Mock(side_effect=[{}, {'phase':phase,'pid':9}, {'phase':'finished','pid':9,'exitcode':exitcode}])
 def test_network_nonzero_preserves_log_diagnostic(self):
  self.setup_net();self.vm.call_qga_command.side_effect=RuntimeError('sharing violation')
  code,msg=self.vm.guest_exec_zs_tools('net','{}',wait=1,retry=10)
  self.assertEqual(2,code);self.assertIn('tail unavailable',msg);self.assertIn('sharing violation',msg)
  self.assertNotIn(self.vm.vm_uuid,ns['_zs_tools_net_pending'])
 def test_network_failed_phase_preserves_diagnostic(self):
  self.setup_net(phase='failed');self.vm.call_qga_command.side_effect=RuntimeError('file not found')
  code,msg=self.vm.guest_exec_zs_tools('net','{}',wait=1,retry=10)
  self.assertEqual(1,code);self.assertIn('without a confirmed result',msg);self.assertIn('file/path not found',msg)
 def test_network_prepare_failure_preserves_original_error(self):
  self.setup_net();self.vm._net_task_control.side_effect=RuntimeError('prepare failed')
  self.vm.call_qga_command.side_effect=RuntimeError('access denied')
  code,msg=self.vm.guest_exec_zs_tools('net','{}',wait=1,retry=10)
  self.assertEqual(1,code);self.assertIn('prepare failed',msg);self.assertIn('access denied',msg)
  self.vm.guest_exec.assert_not_called()
 def test_network_success_skips_log_collection(self):
  self.setup_net(exitcode=0)
  self.assertEqual((0,''),self.vm.guest_exec_zs_tools('net','{}',wait=1,retry=10))
  self.vm.call_qga_command.assert_not_called()
 def test_missing_path_diagnostic(self):
  self.vm.call_qga_command.side_effect=RuntimeError('The system cannot find the file specified')
  self.assertIn('file/path not found',self.vm._get_zs_tools_log_diagnostic())
 def test_localized_errors(self):
  for text,expected in [(u'\u53e6\u4e00\u4e2a\u7a0b\u5e8f\u6b63\u5728\u4f7f\u7528\u6b64\u6587\u4ef6','sharing violation'),(u'\u7cfb\u7edf\u627e\u4e0d\u5230\u6307\u5b9a\u7684\u6587\u4ef6','file/path not found'),(u'\u62d2\u7edd\u8bbf\u95ee','access denied')]:
   self.assertIn(expected,self.vm._zs_tools_log_read_error(RuntimeError(text)))
   for encoding in ('utf-8','gbk'):
    self.assertIn(expected,self.vm._zs_tools_log_read_error(RuntimeError(text.encode(encoding))))
 def test_timeout_and_permission(self):
  for error,expected in [('timed out','QGA timeout'),('Access is denied','access denied'),('unexpected sensitive message','QGA file operation failed')]:
   self.vm.call_qga_command.side_effect=RuntimeError(error)
   self.assertIn(expected,self.vm._get_zs_tools_log_diagnostic())
 def test_read_failure_close_and_fallback(self):
  self.vm.call_qga_command.side_effect=[7,{'position':9},{},RuntimeError('access denied'),{},RuntimeError('not found'),RuntimeError('not found')]
  self.assertIn('access denied',self.vm._get_zs_tools_log_diagnostic())
  self.assertEqual('guest-file-close',self.vm.call_qga_command.call_args_list[4][0][0])
 def test_close_failure_does_not_replace_tail(self):
  self.sequence();self.vm.call_qga_command.side_effect=[7,{'position':9},{},{'buf-b64':b'hi'},RuntimeError('timeout')]
  self.assertIn('hi',self.vm._get_zs_tools_log_diagnostic())
 def test_original_error_and_streams(self):
  self.vm.guest_exec=mock.Mock(return_value={'pid':9})
  self.vm.guest_exec_status_with_timeout=mock.Mock(return_value={'exited':False,'out-data':b'one\r\ntwo','err-data':b'err'})
  self.vm.call_qga_command.side_effect=RuntimeError('sharing violation')
  code,msg=self.vm.guest_exec_zs_tools('host','secret',wait=1,retry=3)
  for part in ['pid 9 not exited','stdout: one\ntwo','stderr: err','tail unavailable','sharing violation','<masked>']:self.assertIn(part,msg)
  self.assertEqual(1,code);self.assertNotIn('secret',msg)
 def test_nonzero_and_success(self):
  self.vm.guest_exec=mock.Mock(return_value={'pid':9});self.sequence()
  self.vm.guest_exec_status_with_timeout=mock.Mock(return_value={'exited':True,'exitcode':2})
  code,msg=self.vm.guest_exec_zs_tools('host','{}',wait=1,retry=3)
  self.assertEqual(2,code);self.assertIn('zs-tools.log tail:',msg)
  self.vm.call_qga_command.reset_mock();self.vm.guest_exec_status_with_timeout.return_value={'exited':True,'exitcode':0}
  self.assertEqual((0,''),self.vm.guest_exec_zs_tools('host','{}',wait=1,retry=3));self.vm.call_qga_command.assert_not_called()
if __name__=='__main__':unittest.main()
