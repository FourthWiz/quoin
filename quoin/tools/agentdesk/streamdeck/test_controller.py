import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('controller',Path(__file__).with_name('controller.py'))
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

class ControllerTests(unittest.TestCase):
    def test_parse_sessions_preserves_spaces_and_excludes_saved_from_live(self):
        with patch.object(c,'run',return_value='project alpha [Created 1m ago]\nold [Created 2d ago] (EXITED - attach to resurrect)'):
            self.assertEqual(c.sessions(),[('project alpha',True),('old',False)])
    def test_never_redirects_to_another_session(self):
        with patch.object(c,'sessions',return_value=[('other',True)]), patch.object(c,'run') as run:
            with self.assertRaises(RuntimeError): c.execute('next-pane',{'session':'gone'})
            run.assert_not_called()
    def test_navigation_is_addressed(self):
        with patch.object(c,'sessions',return_value=[('wanted',True)]), patch.object(c,'run') as run:
            c.execute('next-pane',{'session':'wanted'})
            self.assertEqual(run.call_args.args[0],['zellij','--session','wanted','action','focus-next-pane'])
    def test_project_path_is_one_argument(self):
        project='/tmp/project with spaces; $(nope)'
        with patch.object(c,'apple',return_value='surface-1'), patch.object(c,'sessions',return_value=[('wanted',True)]), patch.object(c,'run') as run:
            c.execute('claude',{'session':'wanted','project':project,'surfaces':{'wanted':'surface-1'}})
            argv=run.call_args.args[0]
            self.assertEqual(argv[argv.index('--cwd')+1],project)
            self.assertEqual(argv[-1],project)
            self.assertNotIn('-c',argv)
    def test_cancel_session_selection_does_not_attach_or_write(self):
        cfg={'project':'/tmp','session':'existing'}
        with patch.object(c,'sessions',return_value=[('test',True)]),patch.object(c,'choose',return_value=None),patch.object(c,'terminal') as terminal,patch.object(c,'save') as save:
            c.execute('sessions',cfg)
            terminal.assert_not_called();save.assert_not_called()
            self.assertEqual(cfg['session'],'existing')
    def test_new_project_clears_old_session(self):
        cfg={'project':'/tmp/old','session':'old'}
        with patch.object(c,'folder',return_value='/tmp/new'),patch.object(c,'save'):
            c.execute('project',cfg)
            self.assertIsNone(cfg['session'])
    def test_no_selection_does_not_mutate_session(self):
        with patch.object(c,'run') as run:
            with self.assertRaises(RuntimeError): c.execute('codex',{'session':None})
            run.assert_not_called()
    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d,patch.object(c,'STATE',Path(d)):
            cfg={'project':'/tmp/тест','session':'test','projects':{}}
            c.save(cfg);self.assertEqual(c.load(),cfg)

if __name__=='__main__': unittest.main()
