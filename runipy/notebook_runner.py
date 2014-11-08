from __future__ import print_function

try:
    # python 2
    from Queue import Empty
except:
    # python 3
    from queue import Empty

import platform
from time import sleep
import logging
import os
import re

from IPython.nbformat.current import NotebookNode
from IPython.kernel import KernelManager


class NotebookError(Exception):
    pass


class NotebookRunner(object):
    # The kernel communicates with mime-types while the notebook
    # uses short labels for different cell types. We'll use this to
    # map from kernel types to notebook format types.

    MIME_MAP = {
        'image/jpeg': 'jpeg',
        'image/png': 'png',
        'text/plain': 'text',
        'text/html': 'html',
        'text/latex': 'latex',
        'application/javascript': 'html',
        'image/svg+xml': 'svg',
    }


    def __init__(self, nb, pylab=False, mpl_inline=False, working_dir = None):
        self.km = KernelManager()

        args = []

        if pylab:
            args.append('--pylab=inline')
            logging.warn('--pylab is deprecated and will be removed in a future version')
        elif mpl_inline:
            args.append('--matplotlib=inline')
            logging.warn('--matplotlib is deprecated and will be removed in a future version')

        cwd = os.getcwd()

        if working_dir:
            os.chdir(working_dir)

        self.km.start_kernel(extra_arguments = args)

        os.chdir(cwd)

        if platform.system() == 'Darwin':
            # There is sometimes a race condition where the first
            # execute command hits the kernel before it's ready.
            # It appears to happen only on Darwin (Mac OS) and an
            # easy (but clumsy) way to mitigate it is to sleep
            # for a second.
            sleep(1)

        self.kc = self.km.client()
        self.kc.start_channels()

        self.shell = self.kc.shell_channel
        self.iopub = self.kc.iopub_channel

        self.nb = nb


    def __del__(self):
        self.kc.stop_channels()
        self.km.shutdown_kernel(now=True)


    def run_code(self, code):
        '''
        Run a notebook cell and update the output of that cell in-place.
        '''
        self.shell.execute(code)
        reply = self.shell.get_msg()
        status = reply['content']['status']
        if status == 'error':
            traceback_text = 'Code raised uncaught exception: \n' + \
                '\n'.join(reply['content']['traceback'])
            logging.info(traceback_text)
        else:
            logging.info('Code returned')

        outs = list()
        prompt_number = -1
        while True:
            try:
                msg = self.iopub.get_msg(timeout=1)
                if msg['msg_type'] == 'status':
                    if msg['content']['execution_state'] == 'idle':
                        break
            except Empty:
                # execution state should return to idle before the queue becomes empty,
                # if it doesn't, something bad has happened
                raise

            content = msg['content']
            msg_type = msg['msg_type']

            # IPython 3.0.0-dev writes pyerr/pyout in the notebook format but uses
            # error/execute_result in the message spec. This does the translation
            # needed for tests to pass with IPython 3.0.0-dev
            notebook3_format_conversions = {
                'error': 'pyerr',
                'execute_result': 'pyout'
            }
            msg_type = notebook3_format_conversions.get(msg_type, msg_type)

            out = NotebookNode(output_type=msg_type)

            if 'execution_count' in content:
                prompt_number = content['execution_count']
                out.prompt_number = content['execution_count']

            if msg_type in ('status', 'pyin', 'execute_input'):
                continue
            elif msg_type == 'stream':
                out.stream = content['name']
                out.text = content['data']
                #print(out.text, end='')
            elif msg_type in ('display_data', 'pyout'):
                for mime, data in content['data'].items():
                    try:
                        attr = self.MIME_MAP[mime]
                    except KeyError:
                        raise NotImplementedError('unhandled mime type: %s' % mime)

                    setattr(out, attr, data)
                #print(data, end='')
            elif msg_type == 'pyerr':
                out.ename = content['ename']
                out.evalue = content['evalue']
                out.traceback = content['traceback']

                #logging.error('\n'.join(content['traceback']))
            elif msg_type == 'clear_output':
                outs = list()
                continue
            else:
                raise NotImplementedError('unhandled iopub message: %s' % msg_type)
            outs.append(out)

        if status == 'error':
            raise NotebookError(traceback_text)

        return (prompt_number, outs)

    def run_cell(self, cell, skip_exceptions):
        '''
        Run a notebook cell and update the output of that cell in-place.
        '''
        logging.info('Running cell:\n%s\n', cell.input)
        try:
            (prompt_number, outs) = self.run_code(cell.input)
        except NotebookError:
            if not skip_exceptions:
                raise
        cell['prompt_number'] = prompt_number
        cell['outputs'] = outs



    def pymarkdown_cell(self, cell):
        '''
        Go looking for {{this}} inside a markdown cell and add this:value to metadata['variables'].
        This is the same thing that the python-markdown extension does.
        '''
        variables = dict()
        for expr in re.finditer("{{(.*?)}}", cell.source):
            code = expr.group(1)
            variables[code] = ""

            logging.info('Running markdown expression:\n%s\n', expr)
            try:
                (prompt_number, outs) = self.run_code(code)
            except NotebookError:
                continue #leave the variable empty

            outs = filter( lambda out : out['output_type'] == 'pyout', outs )
            if len(outs) > 0:
                variables[code] = outs[0]['text']
        cell.metadata['variables'] = variables

    def run_notebook(self, skip_exceptions=False, progress_callback=None):
        '''
        Run all the cells of a notebook in order and update
        the outputs in-place.

        If ``skip_exceptions`` is set, then if exceptions occur in a cell, the
        subsequent cells are run (by default, the notebook execution stops).
        '''
        for ws in self.nb.worksheets:
            for i, cell in enumerate(ws.cells):
                logging.info('celltype: %s\n', cell.cell_type)
                if cell.cell_type == 'code':
                    self.run_cell(cell, skip_exceptions)
                    if progress_callback:
                        progress_callback(i)
                elif cell.cell_type == 'markdown':
                    self.pymarkdown_cell(cell)



    def count_code_cells(self):
        '''
        Return the number of code cells in the notebook
        '''
        return sum(1 for _ in self.iter_code_cells())

