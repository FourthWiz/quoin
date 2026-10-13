import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import monitor
import install_live
import dashboard

class LiveTests(unittest.TestCase):
    def test_parallel_tool_does_not_hide_attention(self):
        a=monitor.transition({},dict(hook_event_name='PermissionRequest',tool_name='Bash',tool_use_id='one'))
        b=monitor.transition(a,dict(hook_event_name='PostToolUse',tool_name='Read',tool_use_id='two'))
        self.assertEqual(b['status'],'attention')
        c=monitor.transition(b,dict(hook_event_name='PostToolUse',tool_name='Bash',tool_use_id='one'))
        self.assertEqual(c['status'],'working')
    def test_question_done_and_interrupt(self):
        a=monitor.transition({},dict(hook_event_name='PreToolUse',tool_name='AskUserQuestion',tool_use_id='q'))
        self.assertEqual(a['status'],'attention')
        self.assertEqual(monitor.transition(a,{'hook_event_name':'Stop'})['status'],'done')
        self.assertEqual(monitor.transition(a,{'hook_event_name':'Interrupt'})['status'],'attention')
    def test_metadata_only_and_no_desktop_threads(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(monitor,'STATE',Path(tmp)):
            data=dict(session_id='test',hook_event_name='UserPromptSubmit',prompt='SECRET',cwd='/work')
            monitor.record('claude',data,environ={},identity=(1,'birth'))
            self.assertFalse((Path(tmp)/'agents').exists())
            monitor.record('claude',data,environ={'ZELLIJ_SESSION_NAME':'test','ZELLIJ_PANE_ID':'1'},identity=(1,'birth'))
            text=next((Path(tmp)/'agents').glob('*.json')).read_text()
            self.assertNotIn('SECRET',text)
            self.assertEqual(json.loads(text)['status'],'working')
    def test_merge_preserves_settings_and_is_idempotent(self):
        old={'theme':'dark','hooks':{'Stop':[{'hooks':[{'type':'command','command':'existing-hook'}]}]}}
        once=install_live.merge(old,'claude',Path('/a b/monitor.py'))
        self.assertEqual(install_live.merge(once,'claude',Path('/a b/monitor.py')),once)
        self.assertEqual(once['hooks']['Stop'][0],old['hooks']['Stop'][0])
        self.assertEqual(once['theme'],'dark')
        self.assertEqual(len(old['hooks']['Stop']),1)
    def test_closed_pane_cannot_focus(self):
        with patch.object(dashboard.c,'load',return_value={}),patch.object(dashboard,'panes',return_value=[]),patch.object(dashboard.c,'apple') as apple:
            with self.assertRaises(RuntimeError):dashboard.focus({'session':'test','pane':'1'})
            apple.assert_not_called()

if __name__=='__main__':unittest.main()
