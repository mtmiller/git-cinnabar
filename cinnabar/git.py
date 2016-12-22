from __future__ import division
import atexit
import contextlib
import logging
import os
import posixpath
import subprocess
import time
from types import GeneratorType
from .util import (
    one,
    Process,
    VersionedDict,
)
from itertools import chain

NULL_NODE_ID = '0' * 40
# An empty git tree has a fixed sha1 which is that of "tree 0\0"
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
# An empty git blob has a fixed sha1 which is that of "blob 0\0"
EMPTY_BLOB = 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391'


class InvalidConfig(Exception):
    pass


def sha1path(sha1, depth=2):
    def parts():
        i = -1
        for i in xrange(0, depth):
            yield sha1[i * 2:i * 2 + 2]
        yield sha1[i * 2 + 2:]
    return '/'.join(parts())


def split_ls_tree(line):
    mode, typ, remainder = line.split(' ', 2)
    sha1, path = remainder.split('\t', 1)
    return mode, typ, sha1, path


class GitProcess(Process):
    KWARGS = Process.KWARGS | set(['config'])

    def __init__(self, *args, **kwargs):
        config = kwargs.get('config', {})

        command = ['git'] + list(chain(*(['-c', '%s=%s' % (n, v)]
                                         for n, v in config.iteritems())))
        command += list(args)

        kwargs.setdefault('logger', args[0])
        super(GitProcess, self).__init__(*command, **kwargs)


class Git(object):
    _update_ref = None
    _fast_import = None
    _notes_depth = {}
    _refs = VersionedDict()
    _initial_refs = _refs._previous
    _config = None
    _replace = {}

    @classmethod
    def register_fast_import(self, fast_import):
        self._fast_import = fast_import
        self._refs = VersionedDict(self._refs)

    @classmethod
    def _close_update_ref(self):
        if self._update_ref:
            retcode = self._update_ref.wait()
            self._update_ref = None
            if retcode:
                raise Exception('git-update-ref failed')

    @classmethod
    def close(self, rollback=False):
        if self._fast_import:
            self._fast_import.close(rollback)
            self._fast_import = None

            self._refs = self._refs.flattened()

    @classmethod
    def iter(self, *args, **kwargs):
        start = time.time()

        proc = GitProcess(*args, **kwargs)
        try:
            for line in proc.stdout or ():
                line = line.rstrip('\n')
                yield line

        finally:
            proc.wait()
            logging.getLogger(args[0]).info('[%d] wall time: %.3fs',
                                            proc.pid, time.time() - start)

    @classmethod
    def run(self, *args):
        return tuple(self.iter(*args, stdout=None))

    @classmethod
    def for_each_ref(self, *patterns):
        if not patterns:
            return
        # Ideally, this would not actually call for-each-ref if all refs
        # matching the given patterns are already known.
        for line in self.iter('for-each-ref', '--format',
                              '%(objectname) %(refname)', *patterns):
            sha1, ref = line.split(' ', 1)
            self._initial_refs[ref] = sha1
            # The ref might have been removed in self._refs
            if ref in self._refs:
                yield self._refs[ref], ref

    @classmethod
    def resolve_ref(self, ref):
        if ref not in self._refs:
            self._initial_refs[ref] = one(
                Git.iter('rev-parse', '--revs-only', ref))
        return self._refs[ref]

    @classmethod
    def cat_file(self, typ, sha1):
        from githg import GitHgHelper
        return GitHgHelper.cat_file(typ, sha1)

    @classmethod
    def ls_tree(self, treeish, path='', recursive=False):
        from githg import GitHgHelper
        if (not isinstance(treeish, Mark) and
                treeish.startswith('refs/')):
            treeish = self.resolve_ref(treeish)
        if isinstance(treeish, Mark) and self._fast_import:
            treeish = self._fast_import.get_mark(treeish)

        if path.endswith('/') or recursive or path == '':
            path = path.rstrip('/')
            for line in GitHgHelper.ls_tree('%s:%s' % (treeish, path),
                                            recursive):
                mode, typ, sha1, p = line
                if path:
                    yield mode, typ, sha1, posixpath.join(path, p)
                else:
                    yield mode, typ, sha1, p
        else:
            # self._fast_import might not be initialized, so use the ls command
            # through the helper instead.
            with GitHgHelper.query('ls', treeish, path) as stdout:
                line = stdout.readline()
                if not line.startswith('missing '):
                    yield split_ls_tree(line[:-1])

    @classmethod
    def diff_tree(self, treeish1, treeish2, path='', detect_copy=False):
        from githg import GitHgHelper
        if path:
            treeish1 = '%s:%s' % (treeish1, path)
            treeish2 = '%s:%s' % (treeish2, path)
        return GitHgHelper.diff_tree(treeish1, treeish2, detect_copy)

    @classmethod
    def update_ref(self, ref, newvalue, oldvalue=None, store=True):
        if not isinstance(newvalue, Mark) and newvalue.startswith('refs/'):
            newvalue = self.resolve_ref(newvalue)
        refs = self._refs if store else self._initial_refs
        if newvalue and newvalue != NULL_NODE_ID:
            refs[ref] = newvalue
            if refs is self._initial_refs and ref in self._refs._deleted:
                self._refs._deleted.remove(ref)
        elif ref in refs:
            del refs[ref]
        if not store:
            return
        if self._fast_import:
            self._fast_import.write(
                'reset %s\n'
                'from %s\n'
                '\n'
                % (ref, newvalue)
            )
            self._fast_import.flush()
            return
        if not self._update_ref:
            self._update_ref = GitProcess('update-ref', '--stdin',
                                          stdin=subprocess.PIPE)
            atexit.register(self._close_update_ref)

        if oldvalue is None:
            update = 'update %s %s\n' % (ref, newvalue)
        else:
            update = 'update %s %s %s\n' % (ref, newvalue, oldvalue)
        self._update_ref.stdin.write(update)

    @classmethod
    def delete_ref(self, ref, oldvalue=None):
        self.update_ref(ref, '0' * 40, oldvalue)

    @classmethod
    def config(self, name, remote=None, values={}):
        if self._config is None:
            proc = GitProcess('config', '-l', '-z')
            data = proc.stdout.read()
            proc.wait()
            self._config = {}
            for l in data.split('\0'):
                if l:
                    k, v = l.split('\n', 1)
                    if k in self._config:
                        self._config[k] += '\0' + v
                    else:
                        self._config[k] = v
        var = name
        value = None
        if name.startswith('cinnabar.'):
            var = 'GIT_%s' % name.replace('.', '_').upper()
            value = os.environ.get(var)
            if value is None and remote:
                var = 'remote.%s.%s' % (remote, name.replace('.', '-'))
                value = self._config.get(var)
        elif name == 'fetch.prune' and remote:
            var = 'remote.%s.prune' % remote
            value = self._config.get(var)
        if value is None:
            var = name
            value = self._config.get(name)
        logging.getLogger('config').info('%s = %r', var, value)
        if values:
            if value in values:
                if isinstance(values, dict):
                    value = values[value]
            else:
                values = ', '.join(repr(v) for v in sorted(values)
                                   if v is not None)
                if value is None:
                    raise InvalidConfig(
                        '%s must be set to one of %s' % (var, values))
                else:
                    raise InvalidConfig(
                        'Invalid value for %s: %s. Valid values: %s' % (
                            var, repr(value), values))
        return value


class Mark(int):
    def __str__(self):
        return ':%d' % self


class EmptyMark(Mark):
    pass


class FastImport(object):
    def __init__(self):
        # We reserve mark 1 for commands without an explicit mark.
        # We get the sha1 from the mark anyways, and the caller, in that case,
        # is expected to be getting that sha1.
        self._last_mark = 1
        self._done = None

    @property
    def _proc(self):
        try:
            return self._real_proc
        except AttributeError:
            from .helper import GitHgHelper
            # Ensure the helper is there.
            if GitHgHelper._helper is GitHgHelper:
                GitHgHelper._helper = False
            with GitHgHelper.query('feature', 'force'):
                pass
            self._real_proc = GitHgHelper._helper
            self.write(
                "feature force\n"
                "feature ls\n"
                "feature notes\n"
            )
            if self._done:
                self.write('feature done\n')

            atexit.register(self.close, rollback=True)

            return self._real_proc

    def send_done(self):
        assert not hasattr(self, '_real_proc')
        self._done = True

    def read(self, length=0):
        self.flush()
        return self._proc.stdout.read(length)

    def readline(self):
        self.flush()
        return self._proc.stdout.readline()

    def write(self, data):
        return self._proc.stdin.write(data)

    def flush(self):
        self._proc.stdin.flush()

    def close(self, rollback=False):
        if not hasattr(self, '_real_proc'):
            return
        if not rollback or self._done is not False:
            self.write('done\n')
            self._done = None
        self.flush()
        from githg import GitHgHelper
        if self._proc is GitHgHelper._helper:
            retcode = self._proc._proc.poll()
        else:
            retcode = self._proc.wait()
        del self._real_proc
        if Git._fast_import == self:
            Git._fast_import = None
        if retcode and not rollback:
            raise Exception('git-fast-import failed')

    def ls(self, dataref, path=''):
        assert not path.endswith('/')
        assert dataref and not isinstance(dataref, EmptyMark)
        self.write('ls %s %s\n' % (dataref, path))
        line = self.readline()
        if line.startswith('missing '):
            return None, None, None, None
        return split_ls_tree(line[:-1])

    def cat_blob(self, dataref):
        assert dataref and not isinstance(dataref, EmptyMark)
        self.write('cat-blob %s\n' % dataref)
        sha1, blob, size = self.readline().split()
        assert blob == 'blob'
        size = int(size)
        content = self.read(size)
        lf = self.read(1)
        assert lf == '\n'
        return content

    def new_mark(self):
        self._last_mark += 1
        return EmptyMark(self._last_mark)

    def get_mark(self, mark):
        self.write('get-mark :%d\n' % mark)
        sha1 = self.read(40)
        lf = self.read(1)
        assert lf == '\n'
        return sha1

    def cmd_data(self, data):
        self.write('data %d\n' % len(data))
        self.write(data)
        self.write('\n')

    def put_blob(self, data='', mark=0):
        self.write('blob\n')
        if mark == 0:
            mark = EmptyMark(1)
        self.write('mark :%d\n' % mark)
        self.cmd_data(data)
        self._done = False
        return self.get_mark(mark)

    @staticmethod
    def _format_committer(author):
        author, epoch, utcoffset = author
        return '%s %d %s%02d%02d' % (
            author,
            epoch,
            '+' if utcoffset >= 0 else '-',
            abs(utcoffset) // 60,
            abs(utcoffset) % 60,
        )

    @contextlib.contextmanager
    def commit(self, ref, committer=('<cinnabar@git>', 0, 0), author=None,
               message='', from_commit=None, parents=(), mark=0):
        if isinstance(parents, GeneratorType):
            parents = tuple(parents)
        _from = None
        from_tree = None
        if parents and parents[0] == from_commit:
            resolved_ref = Git._refs.get(ref)
            if (not isinstance(resolved_ref, Mark) or
                    parents[0] != resolved_ref):
                _from = parents[0]
            merges = parents[1:]
        else:
            _from = NULL_NODE_ID
            merges = parents
            if from_commit:
                mode, typ, from_tree, path = self.ls(from_commit)

        helper = FastImportCommitHelper(self)
        helper.write('commit %s\n' % ref)
        if mark == 0:
            mark = EmptyMark(1)
        helper.write('mark :%d\n' % mark)
        # TODO: properly handle errors, like from the committer being badly
        # formatted.
        if author:
            helper.write('author %s\n' % self._format_committer(author))
        helper.write('committer %s\n' % self._format_committer(committer))
        helper.cmd_data(message)

        if _from:
            helper.write('from %s\n' % _from)
        for merge in merges:
            helper.write('merge %s\n' % merge)
        if from_tree:
            helper.write('M 040000 %s \n' % from_tree)

        yield helper

        helper.flush()
        self.write('\n')
        helper.sha1 = self.get_mark(mark)
        self._done = False
        if mark:
            Git._refs[ref] = helper.sha1
        else:
            del Git._refs[ref]


class FastImportCommitHelper(object):
    def __init__(self, fast_import):
        self._fast_import = fast_import
        self._queue = []

    def write(self, data):
        self._queue.append((self._fast_import.write, data))

    def cmd_data(self, data):
        self._queue.append((self._fast_import.cmd_data, data))

    def flush(self):
        for func, data in self._queue:
            func(data)
        self._queue = []

    def filedelete(self, path):
        self.write('D %s\n' % path)

    MODE = {
        'regular': '644',
        'exec': '755',
        'tree': '040000',
        'symlink': '120000',
        'commit': '160000',
    }

    def filemodify(self, path, sha1=None, typ='regular', content=None):
        assert sha1 and not isinstance(sha1, EmptyMark) or (content and
                                                            typ == 'regular')
        # We may receive the sha1 for an empty blob, even though there is no
        # empty blob stored in the repository. So for empty blobs, use an
        # inline filemodify.
        dataref = 'inline' if sha1 in (EMPTY_BLOB, None) else sha1
        self.write('M %s %s %s\n' % (
            self.MODE[typ],
            dataref,
            path,
        ))
        if sha1 == EMPTY_BLOB:
            self.cmd_data('')
        elif sha1 is None:
            self.cmd_data(content)

    def notemodify(self, commitish, note):
        self.write('N inline %s\n' % commitish)
        self.cmd_data(note)
