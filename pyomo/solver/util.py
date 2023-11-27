#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright (c) 2008-2022
#  National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

import abc
from typing import List

from pyomo.core.expr.visitor import ExpressionValueVisitor, nonpyomo_leaf_types
import pyomo.core.expr as EXPR
from pyomo.core.base.constraint import _GeneralConstraintData, Constraint
from pyomo.core.base.sos import _SOSConstraintData, SOSConstraint
from pyomo.core.base.var import _GeneralVarData, Var
from pyomo.core.base.param import _ParamData, Param
from pyomo.core.base.objective import Objective, _GeneralObjectiveData
from pyomo.common.collections import ComponentMap
from pyomo.common.timing import HierarchicalTimer
from pyomo.core.expr.numvalue import NumericConstant
from pyomo.solver.config import UpdateConfig
from pyomo.solver.results import TerminationCondition, SolutionStatus


def get_objective(block):
    obj = None
    for o in block.component_data_objects(
        Objective, descend_into=True, active=True, sort=True
    ):
        if obj is not None:
            raise ValueError('Multiple active objectives found')
        obj = o
    return obj


def check_optimal_termination(results):
    # TODO: Make work for legacy and new results objects.
    # Look at the original version of this function to make that happen.
    """
    This function returns True if the termination condition for the solver
    is 'optimal', 'locallyOptimal', or 'globallyOptimal', and the status is 'ok'

    Parameters
    ----------
    results : Pyomo Results object returned from solver.solve

    Returns
    -------
    `bool`
    """
    if results.solution_status == SolutionStatus.optimal and (
        results.termination_condition
        == TerminationCondition.convergenceCriteriaSatisfied
    ):
        return True
    return False


def assert_optimal_termination(results):
    """
    This function checks if the termination condition for the solver
    is 'optimal', 'locallyOptimal', or 'globallyOptimal', and the status is 'ok'
    and it raises a RuntimeError exception if this is not true.

    Parameters
    ----------
    results : Pyomo Results object returned from solver.solve
    """
    if not check_optimal_termination(results):
        msg = (
            'Solver failed to return an optimal solution. '
            'Solution status: {}, Termination condition: {}'.format(
                results.solution_status, results.termination_condition
            )
        )
        raise RuntimeError(msg)


class _VarAndNamedExprCollector(ExpressionValueVisitor):
    def __init__(self):
        self.named_expressions = {}
        self.variables = {}
        self.fixed_vars = {}
        self._external_functions = {}

    def visit(self, node, values):
        pass

    def visiting_potential_leaf(self, node):
        if type(node) in nonpyomo_leaf_types:
            return True, None

        if node.is_variable_type():
            self.variables[id(node)] = node
            if node.is_fixed():
                self.fixed_vars[id(node)] = node
            return True, None

        if node.is_named_expression_type():
            self.named_expressions[id(node)] = node
            return False, None

        if type(node) is EXPR.ExternalFunctionExpression:
            self._external_functions[id(node)] = node
            return False, None

        if node.is_expression_type():
            return False, None

        return True, None


_visitor = _VarAndNamedExprCollector()


def collect_vars_and_named_exprs(expr):
    _visitor.__init__()
    _visitor.dfs_postorder_stack(expr)
    return (
        list(_visitor.named_expressions.values()),
        list(_visitor.variables.values()),
        list(_visitor.fixed_vars.values()),
        list(_visitor._external_functions.values()),
    )


class SolverUtils:
    pass


class SubprocessSolverUtils:
    pass


class DirectSolverUtils:
    pass


class PersistentSolverUtils(abc.ABC):
    def __init__(self, only_child_vars=False):
        self._model = None
        self._active_constraints = {}  # maps constraint to (lower, body, upper)
        self._vars = {}  # maps var id to (var, lb, ub, fixed, domain, value)
        self._params = {}  # maps param id to param
        self._objective = None
        self._objective_expr = None
        self._objective_sense = None
        self._named_expressions = (
            {}
        )  # maps constraint to list of tuples (named_expr, named_expr.expr)
        self._external_functions = ComponentMap()
        self._obj_named_expressions = []
        self._update_config = UpdateConfig()
        self._referenced_variables = (
            {}
        )  # var_id: [dict[constraints, None], dict[sos constraints, None], None or objective]
        self._vars_referenced_by_con = {}
        self._vars_referenced_by_obj = []
        self._expr_types = None
        self._only_child_vars = only_child_vars

    @property
    def update_config(self):
        return self._update_config

    @update_config.setter
    def update_config(self, val: UpdateConfig):
        self._update_config = val

    def set_instance(self, model):
        saved_update_config = self.update_config
        self.__init__()
        self.update_config = saved_update_config
        self._model = model
        self.add_block(model)
        if self._objective is None:
            self.set_objective(None)

    @abc.abstractmethod
    def _add_variables(self, variables: List[_GeneralVarData]):
        pass

    def add_variables(self, variables: List[_GeneralVarData]):
        for v in variables:
            if id(v) in self._referenced_variables:
                raise ValueError(
                    'variable {name} has already been added'.format(name=v.name)
                )
            self._referenced_variables[id(v)] = [{}, {}, None]
            self._vars[id(v)] = (
                v,
                v._lb,
                v._ub,
                v.fixed,
                v.domain.get_interval(),
                v.value,
            )
        self._add_variables(variables)

    @abc.abstractmethod
    def _add_params(self, params: List[_ParamData]):
        pass

    def add_params(self, params: List[_ParamData]):
        for p in params:
            self._params[id(p)] = p
        self._add_params(params)

    @abc.abstractmethod
    def _add_constraints(self, cons: List[_GeneralConstraintData]):
        pass

    def _check_for_new_vars(self, variables: List[_GeneralVarData]):
        new_vars = {}
        for v in variables:
            v_id = id(v)
            if v_id not in self._referenced_variables:
                new_vars[v_id] = v
        self.add_variables(list(new_vars.values()))

    def _check_to_remove_vars(self, variables: List[_GeneralVarData]):
        vars_to_remove = {}
        for v in variables:
            v_id = id(v)
            ref_cons, ref_sos, ref_obj = self._referenced_variables[v_id]
            if len(ref_cons) == 0 and len(ref_sos) == 0 and ref_obj is None:
                vars_to_remove[v_id] = v
        self.remove_variables(list(vars_to_remove.values()))

    def add_constraints(self, cons: List[_GeneralConstraintData]):
        all_fixed_vars = {}
        for con in cons:
            if con in self._named_expressions:
                raise ValueError(
                    'constraint {name} has already been added'.format(name=con.name)
                )
            self._active_constraints[con] = (con.lower, con.body, con.upper)
            tmp = collect_vars_and_named_exprs(con.body)
            named_exprs, variables, fixed_vars, external_functions = tmp
            if not self._only_child_vars:
                self._check_for_new_vars(variables)
            self._named_expressions[con] = [(e, e.expr) for e in named_exprs]
            if len(external_functions) > 0:
                self._external_functions[con] = external_functions
            self._vars_referenced_by_con[con] = variables
            for v in variables:
                self._referenced_variables[id(v)][0][con] = None
            if not self.update_config.treat_fixed_vars_as_params:
                for v in fixed_vars:
                    v.unfix()
                    all_fixed_vars[id(v)] = v
        self._add_constraints(cons)
        for v in all_fixed_vars.values():
            v.fix()

    @abc.abstractmethod
    def _add_sos_constraints(self, cons: List[_SOSConstraintData]):
        pass

    def add_sos_constraints(self, cons: List[_SOSConstraintData]):
        for con in cons:
            if con in self._vars_referenced_by_con:
                raise ValueError(
                    'constraint {name} has already been added'.format(name=con.name)
                )
            self._active_constraints[con] = tuple()
            variables = con.get_variables()
            if not self._only_child_vars:
                self._check_for_new_vars(variables)
            self._named_expressions[con] = []
            self._vars_referenced_by_con[con] = variables
            for v in variables:
                self._referenced_variables[id(v)][1][con] = None
        self._add_sos_constraints(cons)

    @abc.abstractmethod
    def _set_objective(self, obj: _GeneralObjectiveData):
        pass

    def set_objective(self, obj: _GeneralObjectiveData):
        if self._objective is not None:
            for v in self._vars_referenced_by_obj:
                self._referenced_variables[id(v)][2] = None
            if not self._only_child_vars:
                self._check_to_remove_vars(self._vars_referenced_by_obj)
            self._external_functions.pop(self._objective, None)
        if obj is not None:
            self._objective = obj
            self._objective_expr = obj.expr
            self._objective_sense = obj.sense
            tmp = collect_vars_and_named_exprs(obj.expr)
            named_exprs, variables, fixed_vars, external_functions = tmp
            if not self._only_child_vars:
                self._check_for_new_vars(variables)
            self._obj_named_expressions = [(i, i.expr) for i in named_exprs]
            if len(external_functions) > 0:
                self._external_functions[obj] = external_functions
            self._vars_referenced_by_obj = variables
            for v in variables:
                self._referenced_variables[id(v)][2] = obj
            if not self.update_config.treat_fixed_vars_as_params:
                for v in fixed_vars:
                    v.unfix()
            self._set_objective(obj)
            for v in fixed_vars:
                v.fix()
        else:
            self._vars_referenced_by_obj = []
            self._objective = None
            self._objective_expr = None
            self._objective_sense = None
            self._obj_named_expressions = []
            self._set_objective(obj)

    def add_block(self, block):
        param_dict = {}
        for p in block.component_objects(Param, descend_into=True):
            if p.mutable:
                for _p in p.values():
                    param_dict[id(_p)] = _p
        self.add_params(list(param_dict.values()))
        if self._only_child_vars:
            self.add_variables(
                list(
                    dict(
                        (id(var), var)
                        for var in block.component_data_objects(Var, descend_into=True)
                    ).values()
                )
            )
        self.add_constraints(
            list(
                block.component_data_objects(Constraint, descend_into=True, active=True)
            )
        )
        self.add_sos_constraints(
            list(
                block.component_data_objects(
                    SOSConstraint, descend_into=True, active=True
                )
            )
        )
        obj = get_objective(block)
        if obj is not None:
            self.set_objective(obj)

    @abc.abstractmethod
    def _remove_constraints(self, cons: List[_GeneralConstraintData]):
        pass

    def remove_constraints(self, cons: List[_GeneralConstraintData]):
        self._remove_constraints(cons)
        for con in cons:
            if con not in self._named_expressions:
                raise ValueError(
                    'cannot remove constraint {name} - it was not added'.format(
                        name=con.name
                    )
                )
            for v in self._vars_referenced_by_con[con]:
                self._referenced_variables[id(v)][0].pop(con)
            if not self._only_child_vars:
                self._check_to_remove_vars(self._vars_referenced_by_con[con])
            del self._active_constraints[con]
            del self._named_expressions[con]
            self._external_functions.pop(con, None)
            del self._vars_referenced_by_con[con]

    @abc.abstractmethod
    def _remove_sos_constraints(self, cons: List[_SOSConstraintData]):
        pass

    def remove_sos_constraints(self, cons: List[_SOSConstraintData]):
        self._remove_sos_constraints(cons)
        for con in cons:
            if con not in self._vars_referenced_by_con:
                raise ValueError(
                    'cannot remove constraint {name} - it was not added'.format(
                        name=con.name
                    )
                )
            for v in self._vars_referenced_by_con[con]:
                self._referenced_variables[id(v)][1].pop(con)
            self._check_to_remove_vars(self._vars_referenced_by_con[con])
            del self._active_constraints[con]
            del self._named_expressions[con]
            del self._vars_referenced_by_con[con]

    @abc.abstractmethod
    def _remove_variables(self, variables: List[_GeneralVarData]):
        pass

    def remove_variables(self, variables: List[_GeneralVarData]):
        self._remove_variables(variables)
        for v in variables:
            v_id = id(v)
            if v_id not in self._referenced_variables:
                raise ValueError(
                    'cannot remove variable {name} - it has not been added'.format(
                        name=v.name
                    )
                )
            cons_using, sos_using, obj_using = self._referenced_variables[v_id]
            if cons_using or sos_using or (obj_using is not None):
                raise ValueError(
                    'cannot remove variable {name} - it is still being used by constraints or the objective'.format(
                        name=v.name
                    )
                )
            del self._referenced_variables[v_id]
            del self._vars[v_id]

    @abc.abstractmethod
    def _remove_params(self, params: List[_ParamData]):
        pass

    def remove_params(self, params: List[_ParamData]):
        self._remove_params(params)
        for p in params:
            del self._params[id(p)]

    def remove_block(self, block):
        self.remove_constraints(
            list(
                block.component_data_objects(
                    ctype=Constraint, descend_into=True, active=True
                )
            )
        )
        self.remove_sos_constraints(
            list(
                block.component_data_objects(
                    ctype=SOSConstraint, descend_into=True, active=True
                )
            )
        )
        if self._only_child_vars:
            self.remove_variables(
                list(
                    dict(
                        (id(var), var)
                        for var in block.component_data_objects(
                            ctype=Var, descend_into=True
                        )
                    ).values()
                )
            )
        self.remove_params(
            list(
                dict(
                    (id(p), p)
                    for p in block.component_data_objects(
                        ctype=Param, descend_into=True
                    )
                ).values()
            )
        )

    @abc.abstractmethod
    def _update_variables(self, variables: List[_GeneralVarData]):
        pass

    def update_variables(self, variables: List[_GeneralVarData]):
        for v in variables:
            self._vars[id(v)] = (
                v,
                v._lb,
                v._ub,
                v.fixed,
                v.domain.get_interval(),
                v.value,
            )
        self._update_variables(variables)

    @abc.abstractmethod
    def update_params(self):
        pass

    def update(self, timer: HierarchicalTimer = None):
        if timer is None:
            timer = HierarchicalTimer()
        config = self.update_config
        new_vars = []
        old_vars = []
        new_params = []
        old_params = []
        new_cons = []
        old_cons = []
        old_sos = []
        new_sos = []
        current_vars_dict = {}
        current_cons_dict = {}
        current_sos_dict = {}
        timer.start('vars')
        if self._only_child_vars and (
            config.check_for_new_or_removed_vars or config.update_vars
        ):
            current_vars_dict = {
                id(v): v
                for v in self._model.component_data_objects(Var, descend_into=True)
            }
            for v_id, v in current_vars_dict.items():
                if v_id not in self._vars:
                    new_vars.append(v)
            for v_id, v_tuple in self._vars.items():
                if v_id not in current_vars_dict:
                    old_vars.append(v_tuple[0])
        elif config.update_vars:
            start_vars = {v_id: v_tuple[0] for v_id, v_tuple in self._vars.items()}
        timer.stop('vars')
        timer.start('params')
        if config.check_for_new_or_removed_params:
            current_params_dict = {}
            for p in self._model.component_objects(Param, descend_into=True):
                if p.mutable:
                    for _p in p.values():
                        current_params_dict[id(_p)] = _p
            for p_id, p in current_params_dict.items():
                if p_id not in self._params:
                    new_params.append(p)
            for p_id, p in self._params.items():
                if p_id not in current_params_dict:
                    old_params.append(p)
        timer.stop('params')
        timer.start('cons')
        if config.check_for_new_or_removed_constraints or config.update_constraints:
            current_cons_dict = {
                c: None
                for c in self._model.component_data_objects(
                    Constraint, descend_into=True, active=True
                )
            }
            current_sos_dict = {
                c: None
                for c in self._model.component_data_objects(
                    SOSConstraint, descend_into=True, active=True
                )
            }
            for c in current_cons_dict.keys():
                if c not in self._vars_referenced_by_con:
                    new_cons.append(c)
            for c in current_sos_dict.keys():
                if c not in self._vars_referenced_by_con:
                    new_sos.append(c)
            for c in self._vars_referenced_by_con.keys():
                if c not in current_cons_dict and c not in current_sos_dict:
                    if (c.ctype is Constraint) or (
                        c.ctype is None and isinstance(c, _GeneralConstraintData)
                    ):
                        old_cons.append(c)
                    else:
                        assert (c.ctype is SOSConstraint) or (
                            c.ctype is None and isinstance(c, _SOSConstraintData)
                        )
                        old_sos.append(c)
        self.remove_constraints(old_cons)
        self.remove_sos_constraints(old_sos)
        timer.stop('cons')
        timer.start('params')
        self.remove_params(old_params)

        # sticking this between removal and addition
        # is important so that we don't do unnecessary work
        if config.update_params:
            self.update_params()

        self.add_params(new_params)
        timer.stop('params')
        timer.start('vars')
        self.add_variables(new_vars)
        timer.stop('vars')
        timer.start('cons')
        self.add_constraints(new_cons)
        self.add_sos_constraints(new_sos)
        new_cons_set = set(new_cons)
        new_sos_set = set(new_sos)
        new_vars_set = set(id(v) for v in new_vars)
        cons_to_remove_and_add = {}
        need_to_set_objective = False
        if config.update_constraints:
            cons_to_update = []
            sos_to_update = []
            for c in current_cons_dict.keys():
                if c not in new_cons_set:
                    cons_to_update.append(c)
            for c in current_sos_dict.keys():
                if c not in new_sos_set:
                    sos_to_update.append(c)
            for c in cons_to_update:
                lower, body, upper = self._active_constraints[c]
                new_lower, new_body, new_upper = c.lower, c.body, c.upper
                if new_body is not body:
                    cons_to_remove_and_add[c] = None
                    continue
                if new_lower is not lower:
                    if (
                        type(new_lower) is NumericConstant
                        and type(lower) is NumericConstant
                        and new_lower.value == lower.value
                    ):
                        pass
                    else:
                        cons_to_remove_and_add[c] = None
                        continue
                if new_upper is not upper:
                    if (
                        type(new_upper) is NumericConstant
                        and type(upper) is NumericConstant
                        and new_upper.value == upper.value
                    ):
                        pass
                    else:
                        cons_to_remove_and_add[c] = None
                        continue
            self.remove_sos_constraints(sos_to_update)
            self.add_sos_constraints(sos_to_update)
        timer.stop('cons')
        timer.start('vars')
        if self._only_child_vars and config.update_vars:
            vars_to_check = []
            for v_id, v in current_vars_dict.items():
                if v_id not in new_vars_set:
                    vars_to_check.append(v)
        elif config.update_vars:
            end_vars = {v_id: v_tuple[0] for v_id, v_tuple in self._vars.items()}
            vars_to_check = [v for v_id, v in end_vars.items() if v_id in start_vars]
        if config.update_vars:
            vars_to_update = []
            for v in vars_to_check:
                _v, lb, ub, fixed, domain_interval, value = self._vars[id(v)]
                if lb is not v._lb:
                    vars_to_update.append(v)
                elif ub is not v._ub:
                    vars_to_update.append(v)
                elif (fixed is not v.fixed) or (fixed and (value != v.value)):
                    vars_to_update.append(v)
                    if self.update_config.treat_fixed_vars_as_params:
                        for c in self._referenced_variables[id(v)][0]:
                            cons_to_remove_and_add[c] = None
                        if self._referenced_variables[id(v)][2] is not None:
                            need_to_set_objective = True
                elif domain_interval != v.domain.get_interval():
                    vars_to_update.append(v)
            self.update_variables(vars_to_update)
        timer.stop('vars')
        timer.start('cons')
        cons_to_remove_and_add = list(cons_to_remove_and_add.keys())
        self.remove_constraints(cons_to_remove_and_add)
        self.add_constraints(cons_to_remove_and_add)
        timer.stop('cons')
        timer.start('named expressions')
        if config.update_named_expressions:
            cons_to_update = []
            for c, expr_list in self._named_expressions.items():
                if c in new_cons_set:
                    continue
                for named_expr, old_expr in expr_list:
                    if named_expr.expr is not old_expr:
                        cons_to_update.append(c)
                        break
            self.remove_constraints(cons_to_update)
            self.add_constraints(cons_to_update)
            for named_expr, old_expr in self._obj_named_expressions:
                if named_expr.expr is not old_expr:
                    need_to_set_objective = True
                    break
        timer.stop('named expressions')
        timer.start('objective')
        if self.update_config.check_for_new_objective:
            pyomo_obj = get_objective(self._model)
            if pyomo_obj is not self._objective:
                need_to_set_objective = True
        else:
            pyomo_obj = self._objective
        if self.update_config.update_objective:
            if pyomo_obj is not None and pyomo_obj.expr is not self._objective_expr:
                need_to_set_objective = True
            elif pyomo_obj is not None and pyomo_obj.sense is not self._objective_sense:
                # we can definitely do something faster here than resetting the whole objective
                need_to_set_objective = True
        if need_to_set_objective:
            self.set_objective(pyomo_obj)
        timer.stop('objective')

        # this has to be done after the objective and constraints in case the
        # old objective/constraints use old variables
        timer.start('vars')
        self.remove_variables(old_vars)
        timer.stop('vars')
