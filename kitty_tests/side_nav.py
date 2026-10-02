#!/usr/bin/env python
# License: GPL v3

import os
import tempfile
from unittest.mock import patch

from kitty.fast_data_types import Region
from kitty.side_nav import SideNav, names_program
from kitty.side_nav_model import SideNavTabInput, build_groups, most_urgent_agent_state, rows_for_groups
from kitty.side_nav_program import program_name
from kitty.side_nav_repo import RepoCache, RepoInfo, find_repo, shorten_path
from kitty.side_nav_tabs import ActivityTracker

from .base import BaseTest


def region(left: int, top: int, right: int, bottom: int) -> Region:
    return Region((left, top, right, bottom, right - left, bottom - top))


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


def tab(
    tab_id: int, cwd: str, title: str = '', is_active: bool = False, agent_state: str = '', needs_attention: bool = False, program: str = ''
) -> SideNavTabInput:
    return SideNavTabInput(tab_id, title or f't{tab_id}', is_active, needs_attention, False, agent_state, cwd, program)


class TestSideNav(BaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.tdir = os.path.realpath(tempfile.mkdtemp())

    def tearDown(self) -> None:
        self.rmtree_ignoring_errors(self.tdir)
        super().tearDown()

    def test_find_repo(self):
        main = os.path.join(self.tdir, 'proj')
        write(os.path.join(main, '.git', 'HEAD'), 'ref: refs/heads/feature/x\n')
        os.makedirs(os.path.join(main, 'src', 'deep'))
        main_git = os.path.join(main, '.git')
        self.ae(find_repo(os.path.join(main, 'src', 'deep')), RepoInfo(main, 'proj', 'feature/x', False, main_git))
        self.ae(find_repo(main), RepoInfo(main, 'proj', 'feature/x', False, main_git))
        self.assertIsNone(find_repo(self.tdir))
        self.assertIsNone(find_repo(''))
        self.assertIsNone(find_repo('relative/path'))

        # A detached HEAD shows a short commit hash
        write(os.path.join(main, '.git', 'HEAD'), '0123456789abcdef0123456789abcdef01234567\n')
        detached = find_repo(main)
        assert detached is not None
        self.ae(detached.branch, '01234567')

        # A linked worktree groups under the main repository's name
        wt = os.path.join(self.tdir, 'elsewhere', 'wt-branch')
        wt_gitdir = os.path.join(main, '.git', 'worktrees', 'wt-branch')
        write(os.path.join(wt, '.git'), f'gitdir: {wt_gitdir}\n')
        write(os.path.join(wt_gitdir, 'HEAD'), 'ref: refs/heads/side-nav\n')
        write(os.path.join(wt_gitdir, 'commondir'), '../..\n')
        # and the same main git dir, its grouping key
        self.ae(find_repo(wt), RepoInfo(wt, 'proj', 'side-nav', True, main_git))

        # A relative gitdir in the .git file is resolved against the worktree
        sub = os.path.join(main, 'vendor', 'lib')
        write(os.path.join(sub, '.git'), 'gitdir: ../../.git/modules/lib\n')
        write(os.path.join(main, '.git', 'modules', 'lib', 'HEAD'), 'ref: refs/heads/main\n')
        self.ae(find_repo(sub), RepoInfo(sub, 'lib', 'main', False, os.path.join(main_git, 'modules', 'lib')))

        # A path through a symlink finds the same repository and key as the real one
        link = os.path.join(self.tdir, 'link')
        os.symlink(main, link)
        via_link = find_repo(os.path.join(link, 'src'))
        assert via_link is not None
        self.ae((via_link.root, via_link.name, via_link.main), (main, 'proj', main_git))

        # A .git file that is not a gitdir pointer is skipped and the search goes up
        bogus = os.path.join(main, 'bogus')
        write(os.path.join(bogus, '.git'), 'not a pointer\n')
        parent = find_repo(bogus)
        assert parent is not None
        self.ae(parent.root, main)

    def test_repo_cache(self):
        main = os.path.join(self.tdir, 'proj')
        write(os.path.join(main, '.git', 'HEAD'), 'ref: refs/heads/one\n')
        now = [100.0]
        cache = RepoCache(ttl=2.0, clock=lambda: now[0])
        repo = cache(main)
        assert repo is not None
        self.ae(repo.branch, 'one')
        write(os.path.join(main, '.git', 'HEAD'), 'ref: refs/heads/two\n')
        now[0] += 1.9
        repo = cache(main)
        assert repo is not None
        self.ae(repo.branch, 'one')  # still cached
        now[0] += 0.2
        repo = cache(main)
        assert repo is not None
        self.ae(repo.branch, 'two')  # expired, so the new branch is seen
        # the cache stays bounded
        cache.max_entries = 4
        for i in range(10):
            cache(f'/nonexistent/{i}')
        self.assertLessEqual(len(cache.entries), 4)

    def test_activity_needs_repeated_output(self):
        t = ActivityTracker()
        # a shell printing its prompt once is not working, however often it is checked
        self.assertFalse(t.is_working(1, 0.1, -1, 10.0))
        self.assertFalse(t.is_working(1, 0.6, -1, 10.5))
        self.assertFalse(t.is_working(1, 1.1, -1, 11.0))
        self.assertFalse(t.is_working(1, 2.0, -1, 11.9))  # quiet, the streak ends
        # output that keeps coming is working, from the second separate output on
        self.assertFalse(t.is_working(2, 0.1, -1, 20.0))
        self.assertTrue(t.is_working(2, 0.1, -1, 20.5))
        self.assertTrue(t.is_working(2, 0.4, -1, 20.8))
        # two outputs closer together than the gap are one burst
        self.assertFalse(t.is_working(3, 0.0, -1, 30.0))
        self.assertFalse(t.is_working(3, 0.0, -1, 30.1))
        # typing echo never counts
        self.assertFalse(t.is_working(4, 0.1, 0.2, 40.0))
        self.assertFalse(t.is_working(4, 0.1, 0.2, 41.0))
        # a redraw after a resize or focus change neither starts nor ends work
        self.assertTrue(t.is_working(2, 0.1, 0.3, 21.0))
        self.assertFalse(t.is_working(5, 0.1, 0.3, 50.0))
        self.assertFalse(t.is_working(5, 0.1, 0.3, 50.5))
        # output well after the stimulus counts again
        self.assertFalse(t.is_working(6, 0.1, 5.0, 60.0))
        self.assertTrue(t.is_working(6, 0.1, 5.5, 60.5))
        self.assertFalse(t.is_working(6, 3.0, 8.0, 63.0))  # quiet now
        # typing drops a streak that had not become work yet
        self.assertFalse(t.is_working(7, 0.1, -1, 70.0))
        self.assertFalse(t.is_working(7, 0.1, 0.2, 75.0))
        self.assertFalse(t.is_working(7, 0.1, 5.0, 80.0))
        t.forget_all_but({2})
        self.ae(set(t.streaks), {2})

    def test_program_name(self):
        for cmdline, expected in (
            (['/home/u/.local/bin/claude', '--resume', 'x'], 'claude'),
            (['/home/u/.nvm/versions/node/v24/bin/node', '/opt/codex/bin/codex.js', '--yolo'], 'codex'),
            (['python3.12', '-u', 'server.py'], 'server'),
            (['-bash'], ''),
            (['/usr/bin/zsh', '-i'], ''),
            (['nvim', 'notes.md'], 'nvim'),
            (['node'], 'node'),
            ([], ''),
            (['python3', '-c', 'import x; x.run()'], 'python'),
            (['python', '-W', 'ignore', 's.py'], 's'),
            (['node', '--require', 'ts-node/register', 'app.js'], 'app'),
            (['python3', '-m', 'http.server', '8000'], 'http'),
            (['npm exec foo'], 'npm'),
            (['node', '/usr/lib/node_modules/@scope/tool/dist/index.js'], 'tool'),
            (['sudo', 'vim', '/etc/hosts'], 'vim'),
            (['env', 'FOO=1', '-i', 'htop'], 'htop'),
            (['/bin/zsh-5.9'], ''),
            (['pypy3', 'bench.py'], 'bench'),
        ):
            self.ae(program_name(cmdline), expected, cmdline)
        self.assertTrue(names_program('top', 'top'))
        self.assertTrue(names_program('codex: fix login', 'codex'))
        self.assertFalse(names_program('desktop notes', 'top'))
        self.assertFalse(names_program('review PR', 'vi'))

    def test_shorten_path(self):
        self.ae(shorten_path('/home/u', '/home/u'), '~')
        self.ae(shorten_path('/home/u/Dev/x', '/home/u'), '~/Dev/x')
        self.ae(shorten_path('/home/user2/x', '/home/u'), '/home/user2/x')
        self.ae(shorten_path('/opt/x', '/home/u'), '/opt/x')

    def test_build_groups(self):
        repos = {
            '/r/a': RepoInfo('/r/a', 'a', 'main'),
            '/r/a/sub': RepoInfo('/r/a', 'a', 'main'),
            '/r/b': RepoInfo('/r/b', 'b', ''),
        }
        calls: list[str] = []

        def repo_for(cwd: str) -> RepoInfo | None:
            calls.append(cwd)
            return repos.get(cwd)

        groups = build_groups(
            (tab(1, '/r/b'), tab(2, '/r/a'), tab(3, '/tmp'), tab(4, '/r/a/sub', is_active=True), tab(5, '/r/b')),
            repo_for,
        )
        # groups appear in the order of their first tab, members keep tab order
        self.ae([g.name for g in groups], ['b', 'a', 'other'])
        self.ae([[t.tab_id for t in g.tabs] for g in groups], [[1, 5], [2, 4], [3]])
        self.ae([g.is_active for g in groups], [False, True, False])
        # the repo lookup runs once per distinct cwd
        self.ae(sorted(calls), ['/r/a', '/r/a/sub', '/r/b', '/tmp'])
        # tabs are numbered by their position in the project, which is what
        # the tab bar shows while that project is selected
        self.ae([[t.index for t in g.tabs] for g in groups], [[1, 2], [1, 2], [1]])
        self.ae(build_groups((), repo_for), ())

    def test_worktrees_share_a_group_and_programs_show_their_branch(self):
        repos = {
            '/src/exp': RepoInfo('/src/exp', 'exp', 'main', False, '/src/exp/.git'),
            '/data/exp/fix': RepoInfo('/data/exp/fix', 'exp', 'fix-login', True, '/src/exp/.git'),
            '/opt/exp': RepoInfo('/opt/exp', 'exp', 'main', False, '/opt/exp/.git'),
        }
        groups = build_groups(
            (
                tab(1, '/src/exp', program='claude'),
                tab(2, '/data/exp/fix', program='codex'),
                tab(3, '/data/exp/fix'),  # a shell shows no branch
                tab(4, '/opt/exp', program='vim'),  # another repository of the same name
            ),
            repos.get,
        )
        # repositories of the same name show where they are
        self.ae([(g.name, [t.tab_id for t in g.tabs]) for g in groups], [('exp · /src', [1, 2, 3]), ('exp · /opt', [4])])
        self.ae([t.branch for t in groups[0].tabs], ['main', 'fix-login', ''])
        rows = rows_for_groups(groups)
        self.ae(
            [(r.kind, r.tab_id) for r in rows],
            [('group', 1), ('tab', 1), ('branch', 1), ('tab', 2), ('branch', 2), ('tab', 3), ('blank', 0), ('group', 4), ('tab', 4), ('branch', 4)],
        )

    def test_rows_and_agent_state(self):
        groups = build_groups((tab(1, '/a'), tab(2, '/b')), lambda cwd: RepoInfo(cwd, cwd[1:], 'main'))
        rows = rows_for_groups(groups)
        self.ae([(r.kind, r.tab_id) for r in rows], [('group', 1), ('tab', 1), ('blank', 0), ('group', 2), ('tab', 2)])
        self.ae(most_urgent_agent_state(('done', 'working', '')), 'working')
        self.ae(most_urgent_agent_state(('Waiting', 'blocked')), 'blocked')
        self.ae(most_urgent_agent_state(('', 'bogus')), '')

    def side_nav(self, height: int = 100, width: int = 200) -> SideNav:
        self.set_options({'side_nav_width': 20})
        with (
            patch('kitty.side_nav.cell_size_for_window', return_value=(10, 20)),
            patch('kitty.side_nav.side_nav_region', return_value=region(0, 0, width, height)),
            patch('kitty.side_nav.set_side_nav_render_data') as srd,
        ):
            sn = SideNav(1)
            self.assertTrue(sn.layout())
            # the line count rounds up so the grid covers the whole region
            self.ae((sn.screen.columns, sn.screen.lines), (width // 10, (height + 19) // 20))
            self.ae(srd.call_args[0][2:], (0, 0, width, sn.screen.lines * 20))
        return sn

    def screen_lines(self, sn: SideNav) -> list[str]:
        return [str(sn.screen.line(i)).rstrip() for i in range(sn.screen.lines)]

    def test_render_click_and_scroll(self):
        sn = self.side_nav(height=110)  # 6 lines, the last one partly visible
        repo_for = {'/a': RepoInfo('/a', 'alpha', 'main'), '/b': RepoInfo('/b', 'beta', 'dev')}.get
        groups = build_groups(
            (
                tab(1, '/a', 'shell'),
                tab(2, '/a', 'claude', agent_state='waiting', program='claude'),
                tab(3, '/b', 'logs'),
                tab(4, '/b', 'a very long tab title that cannot fit', is_active=True, program='vim'),
            ),
            repo_for,
        )
        sn.update(groups)
        # rows: alpha, 1, 2, branch of 2, blank, beta, 3, 4, branch of 4 = 9
        # rows. Only 5 of the 6 lines are fully visible, so the view scrolls
        # until the active tab and its branch are on the fourth and fifth
        # lines rather than on the clipped sixth.
        self.ae(len(sn.rows), 9)
        self.ae(sn.scroll_offset, 4)
        lines = self.screen_lines(sn)
        self.ae(lines[0], '')
        self.ae(lines[1].strip(), '▌beta')
        self.ae(lines[2].split(), ['1', 'logs'])
        # the program sits at the right of a title that does not name it
        self.ae(lines[3].split()[-2:], ['lon…', 'vim'])
        self.ae(lines[4].strip(), '└ dev')
        self.ae(lines[5], '')

        # clicks map screen lines to tabs: a group row focuses its first tab,
        # a branch row the tab above it
        self.ae(sn.tab_id_at(0), 0)  # blank row
        self.ae(sn.tab_id_at(20 * 1 + 5), 3)
        self.ae(sn.tab_id_at(20 * 2), 3)
        self.ae(sn.tab_id_at(20 * 3), 4)
        self.ae(sn.tab_id_at(20 * 4), 4)
        self.ae(sn.tab_id_at(20 * 5), 0)  # past the last row
        self.ae(sn.tab_id_at(20 * 6), 0)  # below the last line
        self.ae(sn.tab_id_at(-5), 0)

        sn.scroll(-100)
        self.ae(sn.scroll_offset, 0)
        lines = self.screen_lines(sn)
        self.ae(lines[0].split(), ['alpha', '●'])
        self.ae(lines[2].split()[:2], ['2', 'claude'])
        self.assertTrue(lines[2].endswith('●'))
        self.ae(lines[3].strip(), '└ main')
        self.ae(sn.tab_id_at(0), 1)  # a group row focuses its first tab
        sn.scroll(100)
        self.ae(sn.scroll_offset, 4)

        # Lines past the last row keep the default background after a redraw,
        # even when the previous render ended on the highlighted active tab.
        sn.update(build_groups((tab(1, '/a', is_active=True),), repo_for))
        self.ae(sn.scroll_offset, 0)
        for y in range(2, sn.screen.lines):
            self.ae(sn.screen.line(y).cursor_from(0).bg, 0, y)
        self.assertNotEqual(sn.screen.line(1).cursor_from(0).bg, 0)
        sn.update(groups)

        # an unchanged model does not redraw
        with patch.object(sn, 'render') as render:
            sn.update(groups)
            render.assert_not_called()

    def test_manual_scroll_survives_updates(self):
        sn = self.side_nav(height=110)
        self.ae(sn.visible_lines, 5)  # the sixth line is clipped, so it does not count
        repo_for = {'/a': RepoInfo('/a', 'alpha', 'main'), '/b': RepoInfo('/b', 'beta', 'dev')}.get

        def groups(active: int, title: str = 'logs'):
            return build_groups(
                (tab(1, '/a'), tab(2, '/a'), tab(3, '/b', title), tab(4, '/b', is_active=active == 4), tab(5, '/b', is_active=active == 5)), repo_for
            )

        sn.update(groups(4))
        # rows: alpha, 1, 2, blank, beta, 3, 4, 5. The active row (index 6)
        # sits on the last fully visible line.
        self.ae(sn.scroll_offset, 2)
        sn.scroll(-100)
        self.ae(sn.scroll_offset, 0)
        # a title change keeps the user's scroll position
        sn.update(groups(4, 'logs updated'))
        self.ae(sn.scroll_offset, 0)
        # a change of the active tab scrolls to it again
        sn.update(groups(5, 'logs updated'))
        self.ae(sn.scroll_offset, 3)

    def test_a_late_branch_row_scrolls_into_view(self):
        sn = self.side_nav(height=60)  # 3 lines
        repo_for = {'/a': RepoInfo('/a', 'alpha', 'main')}.get
        sn.update(build_groups((tab(1, '/a'), tab(2, '/a', is_active=True)), repo_for))
        self.ae(sn.scroll_offset, 0)  # alpha, 1, 2 fit
        # the program in the active tab is found a moment later: its branch row follows
        sn.update(build_groups((tab(1, '/a'), tab(2, '/a', is_active=True, program='vim')), repo_for))
        self.ae(sn.scroll_offset, 1)
        self.ae(self.screen_lines(sn)[2].strip(), '└ main')

    def test_one_line_shows_the_tab_not_its_branch(self):
        sn = self.side_nav(height=20)
        sn.update(build_groups((tab(1, '/a'), tab(2, '/a', is_active=True, program='vim')), lambda cwd: RepoInfo(cwd, 'a', 'main')))
        self.ae(sn.visible_lines, 1)
        self.ae(self.screen_lines(sn)[0].split()[:2], ['2', 't2'])

    def test_resize_keeps_active_tab_visible(self):
        sn = self.side_nav(height=400)  # 20 lines, everything fits
        sn.update(build_groups(tuple(tab(i, '/a', is_active=i == 12) for i in range(1, 13)), lambda cwd: RepoInfo(cwd, 'a', 'main')))
        self.ae(sn.scroll_offset, 0)

        def relayout(height: int) -> None:
            with (
                patch('kitty.side_nav.cell_size_for_window', return_value=(10, 20)),
                patch('kitty.side_nav.side_nav_region', return_value=region(0, 0, 200, height)),
                patch('kitty.side_nav.set_side_nav_render_data'),
            ):
                sn.layout()

        relayout(100)  # 5 lines: the active tab (row 13) must scroll into view
        self.ae(sn.visible_lines, 5)
        self.ae(sn.scroll_offset, 8)
        self.ae(self.screen_lines(sn)[4].split(), ['12', 't12'])
        relayout(400)  # growing again leaves no empty space at the bottom
        self.ae(sn.scroll_offset, 0)

    def test_control_characters_are_not_drawn(self):
        sn = self.side_nav()
        groups = build_groups((tab(1, '/a', 'one\ntwo\rthree\x1b[31m', is_active=True, program='v\x07im'),), lambda cwd: RepoInfo(cwd, 'x\ny', 'b\x08r'))
        sn.update(groups)
        lines = self.screen_lines(sn)
        self.ae(lines[0].strip(), '▌xy')
        # the title fits by its drawn width, so the program keeps its place
        self.ae(lines[1].split(), ['1', 'onetwothre…', 'vim'])
        self.ae(lines[2].strip(), '└ br')

    def test_layout_rejects_tiny_regions(self):
        self.set_options({'side_nav_width': 20})
        with (
            patch('kitty.side_nav.cell_size_for_window', return_value=(10, 20)),
            patch('kitty.side_nav.side_nav_region', return_value=region(0, 0, 0, 0)),
            patch('kitty.side_nav.set_side_nav_render_data') as srd,
        ):
            sn = SideNav(1)
            self.assertFalse(sn.layout())
            srd.assert_not_called()


class TestSideNavWidth(BaseTest):
    def test_resize_steps_and_reset(self):
        from kitty.side_nav_width import MIN_COLS, lowest_width, resized

        self.ae(resized(30, 'wider', 2), 32)
        self.ae(resized(30, 'narrower', 4), 26)
        self.ae(resized(30, 'narrower', -4), 26)
        self.ae(resized(MIN_COLS + 1, 'narrower', 5), MIN_COLS)
        self.ae(resized(30, 'reset', 2), 0)
        # A narrow side_nav_width is itself allowed
        self.ae(lowest_width(5), 5)
        self.ae(lowest_width(30), MIN_COLS)
        self.ae(resized(6, 'narrower', 2, lowest_width(5)), 5)

    def test_action_arguments(self):
        from kitty.options.utils import resize_side_nav

        self.ae(resize_side_nav('resize_side_nav', 'narrower 3'), ('resize_side_nav', ['narrower', 3]))
        self.ae(resize_side_nav('resize_side_nav', ''), ('resize_side_nav', ['wider', 2]))
        self.ae(resize_side_nav('resize_side_nav', 'reset'), ('resize_side_nav', ['reset', 2]))
        with patch('kitty.options.utils.log_error'):
            self.ae(resize_side_nav('resize_side_nav', 'sideways x'), ('resize_side_nav', ['wider', 2]))

    def test_width_is_saved_and_cleared(self):
        from kitty import side_nav_width as snw

        with tempfile.TemporaryDirectory() as tdir, patch.object(snw, 'width_path', lambda: os.path.join(tdir, 'side-nav.json')):
            self.ae(snw.saved_width(30), 0)
            snw.save_width(42, 30)
            self.ae(snw.saved_width(30), 42)
            # Editing side_nav_width makes the option apply again
            self.ae(snw.saved_width(36), 0)
            snw.save_width(0, 30)
            self.ae(snw.saved_width(30), 0)
            self.assertFalse(os.path.exists(snw.width_path()))
            write(snw.width_path(), '{"width": 3, "option": 30}')
            self.ae(snw.saved_width(30), 0)
            write(snw.width_path(), 'not json')
            with patch.object(snw, 'log_error') as log:
                self.ae(snw.saved_width(30), 0)
            log.assert_called_once()


class TestMainProgram(BaseTest):
    def test_the_topmost_program_names_the_window(self):
        from kitty.side_nav_program import main_program

        agy = (10, 1, ['/home/u/.local/bin/agy', '--conversation', 'x'])
        uvx = (11, 10, ['/home/u/.local/bin/uv', 'tool', 'uvx', 'blender-mcp'])
        mcp = (12, 11, ['/cache/bin/python', '/cache/bin/blender-mcp'])
        self.ae(main_program([mcp, uvx, agy]), 'agy')
        # A restored tab runs the agent from a posix shell in the same group
        shell = (9, 1, ['/bin/bash', '--posix'])
        self.ae(main_program([shell, (10, 9, agy[2]), uvx, mcp]), 'agy')
        self.ae(main_program([(20, 1, ['vim', 'a.py'])]), 'vim')
        self.ae(main_program([shell]), '')
        self.ae(main_program([]), '')
        # Separate roots, as in a pipeline, go in pid order
        self.ae(main_program([(31, 1, ['less']), (30, 1, ['grep', 'x'])]), 'grep')

    def test_parent_pid_of_this_process(self):
        from kitty.side_nav_tabs import parent_pid

        self.ae(parent_pid(os.getpid()), os.getppid())
        self.ae(parent_pid(-5), -1)


class TestWorkDir(BaseTest):
    def test_reported_work_dir_wins_over_the_cwd(self):
        from types import SimpleNamespace

        from kitty.side_nav_tabs import SideNavController

        c = SideNavController.__new__(SideNavController)
        c.proc_cwds = {}
        c.programs = {1: 'agent'}
        w = SimpleNamespace(id=1, user_vars={}, child_is_remote=False, screen=SimpleNamespace(last_reported_cwd=''), get_cwd_of_child=lambda: '/start')
        self.set_options({'side_nav_cwd_var': 'work_dir'})
        self.ae(c.work_dir(w), '/start')
        w.user_vars['work_dir'] = '/data/wt'
        self.ae(c.work_dir(w), '/data/wt')
        w.user_vars['work_dir'] = 'file:///data/wt%202'
        self.ae(c.work_dir(w), '/data/wt 2')
        w.user_vars['work_dir'] = 'relative/path'  # not a directory it can use
        self.ae(c.work_dir(w), '/start')
        w.user_vars['work_dir'] = '/' + 'x' * 5000  # longer than any real path
        self.ae(c.work_dir(w), '/start')
        w.user_vars['work_dir'] = '/data/wt'
        c.programs[1] = ''  # the program exited, its report no longer applies
        self.ae(c.work_dir(w), '/start')
        c.programs[1] = 'agent'
        w.child_is_remote = True  # a path on another host
        self.ae(c.work_dir(w), '/start')
        w.child_is_remote = False
        self.set_options({'side_nav_cwd_var': ''})
        w.user_vars['work_dir'] = '/data/wt'
        self.ae(c.work_dir(w), '/start')


class TestStateVar(BaseTest):
    def test_reported_state_needs_the_option(self):
        from types import SimpleNamespace

        from kitty.side_nav_tabs import ActivityTracker, SideNavController

        c = SideNavController.__new__(SideNavController)
        c.activity, c.worked = ActivityTracker(), set()
        w = SimpleNamespace(id=1, user_vars={'agent_state': 'waiting'}, last_resized_at=0, screen=SimpleNamespace(io_times=lambda: (-1, -1)))
        self.set_options({'side_nav_state_var': ''})
        self.ae(c.window_state(w, False, 100.0), '')
        self.set_options({'side_nav_state_var': 'agent_state'})
        self.ae(c.window_state(w, False, 100.0), 'waiting')
