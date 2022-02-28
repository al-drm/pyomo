from io import StringIO
from textwrap import indent

from pyomo.common.collections import Bunch
from pyomo.common.config import (
    ConfigBlock, ConfigValue, NonNegativeInt, In, PositiveInt)
from pyomo.opt import SolverResults, ProblemSense
from pyomo.core.base import Objective, value, minimize
from pyomo.util.model_size import build_model_size_report
from pyomo.contrib.gdpopt.util import a_logger, get_main_elapsed_time

from nose.tools import set_trace

# I don't know where to keep this, just avoiding circular import for now
__version__ = (20, 2, 28)  # Note: date-based version number
_supported_strategies = {
        'LOA': '_logic_based_oa',
        'GLOA': '_global_logic_based_oa',
        'LBB': '_logic_based_branch_and_bound',
        'RIC': '_relaxation_with_integer_cuts',
    }

class _GDPoptAlgorithm(object):
    """Base class for common elements of GDPopt algorithms"""
    _supported_strategies = _supported_strategies

    CONFIG = ConfigBlock("GDPopt")
    CONFIG.declare("iterlim", ConfigValue(
        default=100, domain=NonNegativeInt,
        description="Iteration limit."
    ))
    CONFIG.declare("time_limit", ConfigValue(
        default=600,
        domain=PositiveInt,
        description="Time limit (seconds, default=600)",
        doc="Seconds allowed until terminated. Note that the time limit can "
            "currently only be enforced between subsolver invocations. You may "
            "need to set subsolver time limits as well."
    ))
    CONFIG.declare("strategy", ConfigValue(
        default=None, domain=In(_supported_strategies),
        description="Decomposition strategy to use."
    ))
    CONFIG.declare("tee", ConfigValue(
        default=False,
        description="Stream output to terminal.",
        domain=bool
    ))
    CONFIG.declare("logger", ConfigValue(
        default='pyomo.contrib.gdpopt',
        description="The logger object or name to use for reporting.",
        domain=a_logger
    ))

    def __init__(self, **kwds):
        self.CONFIG = self.CONFIG(kwds.pop('options', {}),
                                  preserve_implicit=True)
        self.CONFIG.set_value(kwds)

        self.LB = -float('inf')
        self.UB = float('inf')
        self.iteration_log = {}
        self.timing = Bunch()
        # TODO: rename these when you understand what they are
        self.master_iteration = 0
        self.mip_iteration = 0
        self.nlp_iteration = 0
        
        self.incumbent = None
        self.primal_bound_improved = False

    def solve(self, model, config):
        """Solve the model.

        Args:
            model (Block): a Pyomo model or block to be solved

        """
        self._log_solver_intro_message(config)

        if self.CONFIG.strategy is not None and \
           self.CONFIG.strategy != config.strategy:
            deprecation_warning("Changing algorithms using "
                                "arguments passed to "
                                "the 'solve' method is deprecated. Please "
                                "specify the algorithm when the solver is "
                                "instantiated.", version="TODO")

        self.pyomo_results = self._get_pyomo_results_object_with_problem_info(
            model, config)
        self.obj_sense = self.pyomo_results.problem.sense

        # Check if this problem actually has any discrete decisions. If not,
        # just solve it.
        problem = self.pyomo_results.problem
        if (problem.number_of_binary_variables == 0 and 
            problem.number_of_integer_variables == 0 and
            problem.number_of_disjunctions == 0):
            return solve_continuous_problem(model, problem)

    def _update_bounds(self, primal=None, dual=None):
        """
        Update bounds correctly depending on objective sense.

        primal: bound from solving subproblem with fixed master solution
        dual: bound from solving master problem (relaxation of original problem)
        """
        if self.obj_sense is minimize:
            if primal is not None:
                self.UB = primal
            if dual is not None:
                self.LB = dual
        else:
            if primal is not None:
                self.LB = primal
            if dual is not None:
                self.UB = dual

    def _update_after_master_problem_solve(self, mip_feasible, obj_expr, 
                                           logger):
        if mip_feasible:
            self._update_bounds(dual=value(obj_expr))
            # TODO: I'm going to change this anyway
            logger.info(
                'ITER {:d}.{:d}.{:d}-MIP: OBJ: {:.10g}  LB: {:.10g}  UB: {:.10g}'.\
                format(
                    self.master_iteration,
                    self.mip_iteration,
                    self.nlp_iteration,
                    value(obj_expr),
                    self.LB, self.UB))
        else:
            # Master problem was infeasible.
            if self.master_iteration == 1:
                config.logger.warning(
                    'GDPopt initialization may have generated poor '
                    'quality cuts.')
            # set optimistic bound to infinity
            if self.objective_sense == minimize:
                self._update_bounds(dual=float('inf'))
            else:
                self._update_bounds(dual=float('-inf'))

    def bounds_converged(self, config):
        if self.LB + config.bound_tolerance >= self.UB:
            config.logger.info(
                'GDPopt exiting on bound convergence. '
                'LB: {:.10g} + (tol {:.10g}) >= UB: {:.10g}'.format(
                    self.LB, config.bound_tolerance, self.UB))
            if self.LB == float('inf') and self.UB == float('inf'):
                self.results.solver.termination_condition = tc.infeasible
            elif self.LB == float('-inf') and self.UB == float('-inf'):
                self.results.solver.termination_condition = tc.infeasible
            else:
                self.results.solver.termination_condition = tc.optimal
            return True
        return False

    def reached_iteration_limit(self, config):
        if self.master_iteration >= config.iterlim:
            config.logger.info(
                'GDPopt unable to converge bounds '
                'after %s master iterations.'
                % (self.master_iteration,))
            config.logger.info(
                'Final bound values: LB: {:.10g}  UB: {:.10g}'.format(
                    self.LB, self.UB))
            self.results.solver.termination_condition = tc.maxIterations
            return True
        return False

    def reached_time_limit(self, config):
        elapsed = get_main_elapsed_time(self.timing)
        if elapsed >= config.time_limit:
            config.logger.info(
                'GDPopt unable to converge bounds '
                'before time limit of {} seconds. '
                'Elapsed: {} seconds'
                .format(config.time_limit, elapsed))
            config.logger.info(
                'Final bound values: LB: {}  UB: {}'.
                format(self.LB, self.UB))
            self.results.solver.termination_condition = tc.maxTimeLimit
            return True
        return False

    def any_termination_criterion_met(self, config):
        return (self.bounds_converged(config) or 
                self.reached_iteration_limit(config) or 
                self.reached_time_limit(config))

    def _get_pyomo_results_object_with_problem_info(self, original_model,
                                                    config):
        """
        Initialize a results object with results.problem information
        """
        results = SolverResults()

        results.solver.name = 'GDPopt %s - %s' % (self.version(),
                                                  config.strategy)
    
        prob = results.problem
        prob.name = original_model.name
        prob.number_of_nonzeros = None  # TODO

        num_of = build_model_size_report(original_model)

        # Get count of constraints and variables
        prob.number_of_constraints = num_of.activated.constraints
        prob.number_of_disjunctions = num_of.activated.disjunctions
        prob.number_of_variables = num_of.activated.variables
        prob.number_of_binary_variables = num_of.activated.binary_variables
        prob.number_of_continuous_variables = num_of.activated.\
                                              continuous_variables
        prob.number_of_integer_variables = num_of.activated.integer_variables

        config.logger.info(
            "Original model has %s constraints (%s nonlinear) "
            "and %s disjunctions, "
            "with %s variables, of which %s are binary, %s are integer, "
            "and %s are continuous." %
            (num_of.activated.constraints,
             num_of.activated.nonlinear_constraints,
             num_of.activated.disjunctions,
             num_of.activated.variables,
             num_of.activated.binary_variables,
             num_of.activated.integer_variables,
             num_of.activated.continuous_variables))

        # Handle missing or multiple objectives, and get sense
        active_objectives = list(original_model.component_data_objects(
            ctype=Objective, active=True, descend_into=True))
        number_of_objectives = len(active_objectives)
        if number_of_objectives == 0:
            logger.warning(
                'Model has no active objectives. Adding dummy objective.')
            main_obj = gdpopt_block.dummy_objective = Objective(expr=1)
        elif number_of_objectives > 1:
            raise ValueError('Model has multiple active objectives.')
        else:
            main_obj = active_objectives[0]
        results.problem.sense = ProblemSense.minimize if main_obj.sense == 1 \
                                else ProblemSense.maximize

        return results

    def _get_final_pyomo_results_object(self):
        """
        Fill in the results.solver information onto the results object
        """
        results = self.pyomo_results
        # Finalize results object
        results.problem.lower_bound = self.LB
        results.problem.upper_bound = self.UB
        results.solver.iterations = self.master_iteration
        results.solver.timing = self.timing
        results.solver.user_time = self.timing.total
        results.solver.wallclock_time = self.timing.total

        return results

    """Support use as a context manager under current solver API"""
    def __enter__(self):
        return self

    def __exit__(self, t, v, traceback):
        pass

    def available(self, exception_flag=True):
        """Check if solver is available.

        TODO: For now, it is always available. However, sub-solvers may not
        always be available, and so this should reflect that possibility.

        """
        return True

    def license_is_valid(self):
        return True

    def version(self):
        """Return a 3-tuple describing the solver version."""
        return __version__

    def _log_solver_intro_message(self, config):
        config.logger.info(
            "Starting GDPopt version %s using %s algorithm"
            % (".".join(map(str, self.version())), config.strategy)
        )
        mip_args_output = StringIO()
        nlp_args_output = StringIO()
        minlp_args_output = StringIO()
        lminlp_args_output = StringIO()
        config.mip_solver_args.display(ostream=mip_args_output)
        config.nlp_solver_args.display(ostream=nlp_args_output)
        config.minlp_solver_args.display(ostream=minlp_args_output)
        config.local_minlp_solver_args.display(ostream=lminlp_args_output)
        mip_args_text = indent(mip_args_output.getvalue().rstrip(), prefix=" " *
                               2 + " - ")
        nlp_args_text = indent(nlp_args_output.getvalue().rstrip(), prefix=" " *
                               2 + " - ")
        minlp_args_text = indent(minlp_args_output.getvalue().rstrip(),
                                 prefix=" " * 2 + " - ")
        lminlp_args_text = indent(lminlp_args_output.getvalue().rstrip(),
                                  prefix=" " * 2 + " - ")
        mip_args_text = "" if len(mip_args_text.strip()) == 0 else \
                        "\n" + mip_args_text
        nlp_args_text = "" if len(nlp_args_text.strip()) == 0 else \
                        "\n" + nlp_args_text
        minlp_args_text = "" if len(minlp_args_text.strip()) == 0 else \
                          "\n" + minlp_args_text
        lminlp_args_text = "" if len(lminlp_args_text.strip()) == 0 else \
                           "\n" + lminlp_args_text
        config.logger.info(
            """
            Subsolvers:
            - MILP: {milp}{milp_args}
            - NLP: {nlp}{nlp_args}
            - MINLP: {minlp}{minlp_args}
            - local MINLP: {lminlp}{lminlp_args}
            """.format(
                milp=config.mip_solver,
                milp_args=mip_args_text,
                nlp=config.nlp_solver,
                nlp_args=nlp_args_text,
                minlp=config.minlp_solver,
                minlp_args=minlp_args_text,
                lminlp=config.local_minlp_solver,
                lminlp_args=lminlp_args_text,
            ).strip()
        )
        to_cite_text = """
        If you use this software, you may cite the following:
        - Implementation:
        Chen, Q; Johnson, ES; Siirola, JD; Grossmann, IE.
        Pyomo.GDP: Disjunctive Models in Python.
        Proc. of the 13th Intl. Symposium on Process Systems Eng.
        San Diego, 2018.
        """.strip()
        if config.strategy == "LOA":
            to_cite_text += "\n"
            to_cite_text += """
            - LOA algorithm:
            Türkay, M; Grossmann, IE.
            Logic-based MINLP algorithms for the optimal synthesis of process 
            networks. Comp. and Chem. Eng. 1996, 20(8), 959–978.
            DOI: 10.1016/0098-1354(95)00219-7.
            """.strip()
        elif config.strategy == "GLOA":
            to_cite_text += "\n"
            to_cite_text += """
            - GLOA algorithm:
            Lee, S; Grossmann, IE.
            A Global Optimization Algorithm for Nonconvex Generalized 
            Disjunctive Programming and Applications to Process Systems.
            Comp. and Chem. Eng. 2001, 25, 1675-1697.
            DOI: 10.1016/S0098-1354(01)00732-3.
            """.strip()
        elif config.strategy == "LBB":
            to_cite_text += "\n"
            to_cite_text += """
            - LBB algorithm:
            Lee, S; Grossmann, IE.
            New algorithms for nonlinear generalized disjunctive programming.
            Comp. and Chem. Eng. 2000, 24, 2125-2141.
            DOI: 10.1016/S0098-1354(00)00581-0.
            """.strip()
        config.logger.info(to_cite_text)

    _metasolver = False
