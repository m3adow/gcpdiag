# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Runbook command: find potential issues in GCP projects."""

import ast
import inspect
import logging
import sys
import textwrap
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Set, final

import googleapiclient.errors

from gcpdiag import caching, config, models, utils
from gcpdiag.runbook import exceptions, flags, report, util
from gcpdiag.runbook.constants import (END_MESSAGE, STEP_LABEL, STEP_MESSAGE,
                                       StepType)

DiagnosticTreeRegister: Dict[str, 'DiagnosticTree'] = {}


class Step:
  """
  Represents a step in a diagnostic or runbook process.
  """
  steps: List['Step']
  interface: report.InteractionInterface
  template: str

  def __init__(self,
               parent: 'Step' = None,
               context=None,
               interface=None,
               parameters: models.Parameter = None,
               step_type=StepType.AUTOMATED,
               name=None):
    """
    Initializes a new instance of the Step class.
    """
    self.name = name or util.generate_random_string()
    self.steps = []
    self.context = context
    self._parameters = parameters
    self.interface = interface
    self.type = step_type
    self.prompts: models.Messages = models.Messages()
    self.output = None
    self.product = self.__module__.split('.')[-2]
    self.id = '.'.join([self.__module__, self.__class__.__name__])
    self.doc_file_name = util.pascal_case_to_kebab_case(self.__class__.__name__)
    # allow developers to set this
    if parent:
      parent.add_child(child=self)
    self.set_prompts()

  @property
  def run_id(self):
    return '.'.join([self.id, self.name])

  @property
  def op(self) -> models.Operation:
    return self._operation

  @final
  def hook_execute(self, context: models.Context, interface):
    """
    Executes the step using the given context and interface.

    Parameters:
        context: The context in which the step is executed.
        interface: The interface used for interactions during execution.
    """
    self.context = context if context else self.context
    self._parameters = context.parameters  # pylint: disable=protected-access
    self.interface = interface if interface else self.interface
    self.load_prompts()
    self._operation = models.Operation(message=self.prompts,
                                       parameters=self._parameters)
    self.execute()

  def execute(self):
    """Executes the step using the given context and interface."""
    pass

  def set_prompts(self, prompt: models.Messages = None):

    # override existing messages
    if prompt:
      self.prompts.update(prompt)

  def load_prompts(self):
    if hasattr(self, 'template'):
      name = getattr(self, 'template').split('::')
    else:
      return

    if len(name) == 2:
      file_name, block_name = name[0], name[1]
      self.prompts.update(
          util.load_template_block(module_name=self.__module__,
                                   block_name=block_name,
                                   file_name=file_name))
    if len(name) == 3:
      file_name, block_name = name[1], name[2]
      self.prompts.update(
          util.load_template_block(module_name=name[0],
                                   block_name=block_name,
                                   file_name=file_name))

  def add_child(self, child):
    """Child steps"""
    if self.steps and self.steps[-1].type == StepType.END:
      raise exceptions.InvalidStepOperation(
          'Unable to add a new intermediary step. The last step in the parent sequence is '
          f'{self.steps[-1].__class__.__name__}, indicating the sequence has already been '
          'concluded. A step cannot be added after a terminal step like '
          f'{self.steps[-1].__class__.__name__}.')
    self.steps.append(child)

  def find_step(self, step: 'Step'):
    """Find a step by ID"""
    if self == step:
      return self
    for child in self.steps:
      result = child.find_step(step)
      if result:
        return result
    return None

  def __hash__(self) -> int:
    return hash((self.run_id, self.type))

  @property
  def label(self):
    label = self.prompts.get_msg(STEP_LABEL)
    if 'NOTICE' in label:
      label = util.pascal_case_to_title(self.__class__.__name__)
    return label

  @property
  def long_desc(self):
    long_desc = None
    doc_lines = self.__doc__.splitlines()
    if len(doc_lines) >= 3:
      if doc_lines[1]:
        raise ValueError(
            f'DT {self.__class__.__name__} has a non-empty second line in the module docstring'
        )
      long_desc = '\n'.join(doc_lines[2:])
    return long_desc

  @property
  def short_desc(self):
    return self.__doc__.splitlines()[0]

  @property
  def doc_url(self):
    """Returns the documentation URL for the step."""
    return f'https://gcpdiag.dev/runbook/steps/{self.product}/{self.doc_file_name}'


class StartStep(Step):
  """Start Event of a Diagnotic tree"""

  def __init__(self,
               context=None,
               interface=None,
               parameters: models.Parameter = None):
    super().__init__(context, interface, parameters, step_type=StepType.START)


class CompositeStep(Step):
  """Composite Events of a Diagnotic tree"""

  def __init__(self,
               context=None,
               interface=None,
               parameters: models.Parameter = None):
    super().__init__(context,
                     interface,
                     parameters,
                     step_type=StepType.COMPOSITE)


class EndStep(Step):
  """End Event of a Diagnostic Tree"""

  def __init__(self,
               context=None,
               interface=None,
               parameters: models.Parameter = None):
    super().__init__(context, interface, parameters, step_type=StepType.END)

  def execute(self):
    if not config.get(flags.AUTO):
      response = self.interface.prompt(task=self.interface.CONFIRMATION,
                                       message='Is your issue resolved?')
      if response == self.interface.NO:
        self.interface.prompt(message=END_MESSAGE)


class Gateway(Step):
  """
  Represents a decision point in a workflow, determining which path to take based on a condition.
  """

  def __init__(self, step_type=StepType.GATEWAY, context=None, interface=None):
    """
    Initializes a new instance of the Gateway step.
    """
    super().__init__(context=context,
                     interface=interface,
                     step_type=StepType.GATEWAY)
    self.type = step_type


class RunbookRule(type):
  """Metaclass for automatically registering subclasses of DiagnosticTree
  in a registry for easy lookup and instantiation based on product/rule ID."""

  def __new__(mcs, name, bases, namespace):
    new_class = super().__new__(mcs, name, bases, namespace)
    if name != 'DiagnosticTree' and bases[0] == DiagnosticTree:
      rule_id = util.runbook_name_parser(name)
      product = namespace.get('__module__', '').split('.')[-2].lower()
      DiagnosticTreeRegister[f'{product}/{rule_id}'] = new_class
    return new_class


class DiagnosticTree(metaclass=RunbookRule):
  """Represents a diagnostic tree for troubleshooting."""
  product: str
  name: str
  start: StartStep
  parameters: Dict[str, Dict]
  steps: List[Step]

  def __init__(self, context: models.Context):
    self.id = f'{self.__module__}.{self.__class__.__name__}'
    self.context = context
    self.product = self.__module__.rsplit('.')[-2].lower()
    self.doc_file = util.pascal_case_to_kebab_case(self.__class__.__name__)
    self.name = f'{self.product}/{util.runbook_name_parser(self.__class__.__name__)}'
    self.steps: List = []
    self.parameters = self.parameters if hasattr(self, 'parameters') else {}

  def set_context(self, context: models.Context):
    """Sets the execution context for the diagnostic tree."""
    self.context = context

  def add_step(self, parent: Step, child: Step):
    """Adds an intermidate diagnostic step to the tree."""
    if self.start is None:
      raise ValueError('Start step is empty. Set start step with'
                       ' builder.add_start() or tree.add_start()')
    if parent is None:
      raise ValueError('You can\'t add a child to a `NoneType` parent')
    # to avoid disjoint trees, we first check that the parent is a child of start.
    parent_step = self.start.find_step(parent)
    if parent_step:
      parent_step.add_child(child)
    else:
      raise ValueError(
          f'Parent step with {parent.run_id} not found. Add parent first')

  def add_start(self, step: StartStep):
    """Adds a diagnostic step to the tree."""
    if hasattr(self, 'start') and getattr(self, 'start'):
      raise ValueError('start already exist')
    self.start = step

  def add_end(self, step: EndStep):
    """Adds the default end step of this tree which is invoked iff all child steps are exectued."""
    if self.start and self.start.steps:
      if self.start.steps[-1].type == StepType.END:
        raise ValueError('end already exist')
    self.start.add_child(child=step)

  def find_step(self, step_id):
    return self.start.find_step(step_id)

  @final
  def hook_build_tree(self):
    try:
      self.build_tree()
    except exceptions.InvalidDiagnosticTree as err:
      logging.warning('%s: %s while constructing runbook rule: %s',
                      type(err).__name__, err, self)

    if not self.start:
      raise exceptions.InvalidDiagnosticTree(
          'The diagnostic tree is invalid because it contains any Start point')

    if not self.start.steps:
      raise exceptions.InvalidDiagnosticTree(
          'The diagnostic tree is invalid because it contains only '
          'a Start method without any intermediate steps. '
          'Please ensure your tree includes at least one intermediate step '
          'between the Start method and the end point.')
    # if the tree hasn't be concluded with an endstep.
    # Add the default step
    if self.start.steps[-1].type != StepType.END:
      self.start.add_child(EndStep())

  def build_tree(self):
    """Constructs the diagnostic tree."""
    raise NotImplementedError

  def __hash__(self):
    return str(self.name).__hash__()

  def __str__(self):
    return self.name

  @property
  def long_desc(self):
    long_desc = None
    doc_lines = self.__class__.__doc__.splitlines()
    if len(doc_lines) >= 3:
      if doc_lines[1]:
        raise ValueError(
            f'DT {self.__class__.__name__} has a non-empty second line in the module docstring'
        )
      long_desc = '\n'.join(doc_lines[2:])
    return long_desc

  @property
  def short_desc(self):
    return self.__doc__.splitlines()[0]

  @property
  def doc_url(self) -> str:
    """Returns the documentation URL for the diagnostic tree."""
    return f'https://gcpdiag.dev/runbook/diagnostic-trees/{self.name}'


class DiagnosticEngine:
  """Loads and Executes diagnostic trees.

  This class is responsible for loading and executing diagnostic trees
  based on provided rule name. It manages the diagnostic process, from
  validating required parameters to traversing and executing diagnostic
  steps.

  Attributes:
    _dt: Optional[DiagnosticTree] The current diagnostic tree being executed.
  """

  def __init__(self, rm: report.ReportManager = None, interface=None) -> None:
    """Initializes the DiagnosticEngine with required managers."""
    self.rm = rm or report.TerminalReportManager()
    self.interface = interface or report.InteractionInterface()

  def load_rule(self, name: str) -> None:
    """Loads a diagnostic tree by name ex gce/ssh.

    Attempts to retrieve a diagnostic tree registered under the given name.
    Exits the program if the tree is not found.

    Tree are registered onload of the class definition see RunbookRule metaclass

    Args:
      name: The name of the diagnostic tree to load.
    """
    try:
      # allow users to use pascal, snake , camel case to locate a rule
      # ex: product/gce-runbook, product.gce_runbook, product.gce_runbook,
      name = util.runbook_name_parser(name)
      self.dt = DiagnosticTreeRegister.get(name)
      if not self.dt:
        raise exceptions.DiagnosticTreeNotFound
    except exceptions.DiagnosticTreeNotFound as e:
      logging.error(e)
      sys.exit(2)

  def _check_required_paramaters(self, tree: DiagnosticTree):
    missing_parameters = {
        key: value.get('help', '')
        for key, value in tree.parameters.items()
        if value.get('required', False) and key not in tree.context.parameters  # pylint: disable=protected-access
    }
    if missing_parameters:
      missing_param_str = '\n'.join(
          f"`{key}`: '{value}'" for key, value in missing_parameters.items())
      logging.error(
          'Missing %s required %s. Please provide the following:\n\n%s\n\nExiting program',
          len(missing_parameters),
          'parameter' if len(missing_parameters) == 1 else 'parameters',
          missing_param_str)
      sys.exit(2)

  def set_default_parameters(self, dt):
    '''Get start and end times from user input or set to defaults.'''

    end_time = dt.context.parameters.get(flags.END_TIME_UTC,
                                         datetime.now(timezone.utc))
    # Convert from string/epoch to datetime if necessary
    if isinstance(end_time, str):
      end_time = util.parse_time_input(end_time)

    start_time = dt.context.parameters.get(flags.START_TIME_UTC,
                                           end_time - timedelta(hours=8))

    if isinstance(start_time, str):
      start_time = util.parse_time_input(start_time)

    dt.context.parameters[flags.END_TIME_UTC] = end_time
    dt.context.parameters[flags.START_TIME_UTC] = start_time

  def run_diagnostic_tree(self, context: models.Context) -> None:
    """Executes the loaded diagnostic tree within a given context.

    Validates required parameters, builds the tree, and executes its steps.
    Handles exceptions during execution and generates a report upon completion.

    Args:
      context: The execution context for the diagnostic tree.
    """
    if not self.dt or not callable(self.dt):
      logging.error('Could not instantiate Diagnostic Tree')
      sys.exit(2)

    dt = self.dt(context)

    self._check_required_paramaters(dt)
    self.set_default_parameters(dt)
    self.rm.tree = dt
    self.interface.set_dt(dt)
    self.interface.rm = self.rm
    self.interface.output.display_runbook_description(dt)

    try:
      dt.hook_build_tree()
      self.find_path_dfs(step=dt.start, context=context)

    except (utils.GcpApiError, googleapiclient.errors.HttpError, RuntimeError,
            ValueError, KeyError, exceptions.InvalidDiagnosticTree) as err:
      if isinstance(err, googleapiclient.errors.HttpError):
        err = utils.GcpApiError(err)
      logging.warning('%s: %s while processing runbook rule: %s',
                      type(err).__name__, err, dt)

  def find_path_dfs(self,
                    step: Step,
                    context: models.Context,
                    interface=None,
                    executed_steps: Set = None):
    """Depth-first search to traverse and execute steps in the diagnostic tree.

    Args:
      step: The current step to execute.
      context: The execution context.
      interface: The interface managing user interactions.
      executed_steps: A set of executed step IDs to avoid cycles.
    """
    if executed_steps is None:
      executed_steps = set()

    if not interface:
      interface = self.interface

    self.run_step(step=step, context=context, interface=interface)
    executed_steps.add(step)
    for child in step.steps:  # Iterate over the children of the current step
      if child not in executed_steps:
        self.find_path_dfs(step=child,
                           context=context,
                           interface=interface,
                           executed_steps=executed_steps)

    return executed_steps

  def run_step(self, step: Step, context: models.Context, interface):
    """Executes a single step, handling user decisions for step re-evaluation or termination.

    Args:
      step: The diagnostic step to execute.
      context: The execution context.
      interface: The interface for interaction.
    """
    if step.prompts.get(STEP_MESSAGE):
      step_message = step.prompts[STEP_MESSAGE]
    else:
      step_message = step.execute.__doc__
    interface.output.prompt(step=step.type.value, message=step_message)

    user_input = self._run(step, context, interface)
    while True:
      if user_input is interface.output.RETEST:
        interface.output.prompt(step=interface.output.RETEST,
                                message='Re-evaluating recent failed step')
        with caching.bypass_cache():
          user_input = self._run(step, context, interface)
      elif step.type == StepType.END:
        self.rm.generate_report()
        break
      elif user_input is interface.output.ABORT:
        logging.info('Terminating Runbook\n')
        sys.exit(2)
      elif step.type == StepType.START and (
        self.rm.results.get(step.run_id) is not None and \
          self.rm.results[step.run_id].status == 'skipped'):
        logging.info('Start Step was skipped. Can\'t proceed ...\n')
        sys.exit(2)
      elif user_input is interface.output.CONTINUE:
        break
      elif (user_input is not interface.output.RETEST and
            user_input is not interface.output.CONTINUE and
            user_input is not interface.output.ABORT):
        return user_input

  def _run(self, step: Step, context: models.Context, interface):
    start = datetime.now(timezone.utc).timestamp()
    step.hook_execute(context, interface)
    end = datetime.now(timezone.utc).timestamp()
    if self.rm.results.get(step.run_id):
      self.rm.results[step.run_id].start_time_utc = start
      self.rm.results[step.run_id].end_time_utc = end
      return self.rm.results[step.run_id].prompt_response
    return None


class ExpandTreeFromAst(ast.NodeVisitor):
  """Builds a Diagnostic Tree by traversing the Abstract Syntax Tree

  This is required to be able to get a full flow of all possible steps
  regardless of conditions required to trigger during runtime execution.
  """

  def __init__(self, tree=None):
    self.tree: DiagnosticTree = tree(None) or DiagnosticTree(None)
    self.parent: OrderedDict = OrderedDict()
    self.current_class = None
    # Track instances to map variable names to their classes
    self.instances = {}

  # pylint: disable=invalid-name
  def visit_Assign(self, node):
    # Track class instances
    if isinstance(node.value, ast.Call) and hasattr(node.value.func, 'id'):
      for target in node.targets:
        if isinstance(target, ast.Name):
          clazz = find_class_globally(node.value.func.id)
          if not clazz:
            continue
          o = clazz() if isinstance(clazz, type) else clazz
          self.instances.setdefault(target.id, o)
          self.instances.setdefault(node.value.func.id, o)
    # Check for is there are more nesting. like gce_cs.spec.SomeStep()
    if isinstance(node.value, ast.Call) and hasattr(node.value.func, 'attr'):
      for target in node.targets:
        if isinstance(target, ast.Name):
          clazz = find_class_globally(node.value.func.attr)
          if not clazz:
            continue
          o = clazz() if isinstance(clazz, type) else clazz
          self.instances.setdefault(target.id, o)
          self.instances.setdefault(node.value.func.attr, o)
    self.generic_visit(node)

  # pylint: disable=invalid-name
  def visit_Call(self, node):
    if isinstance(node.func,
                  ast.Attribute) and node.func.attr in ('add_child', 'add_step',
                                                        'add_start', 'add_end'):
      child = None
      if len(node.args) == 1:
        node.keywords.append(ast.keyword('step', node.args[0]))
      if len(node.args) == 2:
        node.keywords.append(ast.keyword('parent', node.args[0]))
        node.keywords.append(ast.keyword('child', node.args[0]))
      if node.keywords:
        for kw in node.keywords:
          arg_name = kw.arg
          step = kw.value
          if arg_name == 'parent':
            p = find_class_globally(self.instances[step.id])
            p = p() if isinstance(p, type) else p
            self.parent.setdefault(self.instances[step.id], p)
          if arg_name in ('child', 'step'):
            if isinstance(step, ast.Name) and step.id in self.instances:
              # Map variable name to class instance if possible
              clazz = self.instances.get(step.id)
              if not clazz:
                clazz = find_class_globally(step.id)
                if not clazz:
                  continue
              clazz = clazz() if isinstance(clazz, type) else clazz
              self.instances.setdefault(step.id, clazz)
              child = clazz  # This is a direct class name
            elif isinstance(step, ast.Call) and hasattr(step.func, 'id'):
              clazz = self.instances.get(step.func.id)
              if not clazz:
                clazz = find_class_globally(step.func.id)
                if not clazz:
                  continue
              clazz = clazz() if isinstance(clazz, type) else clazz
              self.instances.setdefault(step.func.id, clazz)
              child = clazz  # This is a direct class name
            # if argment of call is instantiated directly and has module attr.
            elif isinstance(step, ast.Call) and hasattr(step.func, 'attr'):
              clazz = self.instances.get(step.func.attr)
              if not clazz:
                clazz = find_class_globally(step.func.attr)
                if not clazz:
                  continue
              clazz = clazz() if isinstance(clazz, type) else clazz
              self.instances.setdefault(step.func.attr, clazz)
              child = clazz  # This is a direct class name

      if node.func.attr in ('add_start'):
        self.tree.add_start(child)
        self.visit_ast_nodes(child.__class__)

      if node.func.attr in ('add_end'):
        self.tree.add_end(child)
        self.visit_ast_nodes(child.__class__)

      if node.func.attr in ('add_step'):
        _, p = self.parent.popitem()
        self.tree.add_step(parent=p, child=child)
        self.visit_ast_nodes(child.__class__)

      if node.func.attr in ('add_child'):
        o = self.instances.get(self.current_class)
        self.tree.add_step(parent=o, child=child)
        self.generic_visit(node)

  def visit_ClassDef(self, node):
    # Initialize or clear the list of add_child calls for this class
    self.current_class = node.name
    self.generic_visit(node)
    self.current_class = None

  def visit_ast_nodes(self, func):
    source_code = inspect.getsource(func)
    tree = ast.parse(textwrap.dedent(source_code))
    self.generic_visit(tree)
    return self.tree


def find_class_globally(class_name):
  try:
    if not isinstance(class_name, str) and isinstance(class_name, object):
      return class_name
  except TypeError:
    pass
  # First, check the current global namespace
  cls = globals().get(class_name)
  if cls:
    return cls

  # If not found, check each imported module
  for _, module in sys.modules.items():
    try:
      cls = getattr(module, class_name, None)
      if cls and inspect.isclass(cls) and issubclass(cls, Step):
        return cls
    except AttributeError:
      continue
  return None
