A pre-plugin quoin arm for the benchmark harness tests: `installer.py`, the hook
scripts and `CLAUDE.md` exactly as they were at the `legacy` tag. The harness
installs an arm file by file into a sandbox `~/.claude`, which only pre-plugin
trees support, so its tests run against this copy instead of the live tree.
