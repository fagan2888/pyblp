"""Market-level BLP problem functionality."""

import functools
from typing import List, Tuple, Union

import numpy as np

from .market import Market
from .. import exceptions, options
from ..configurations.iteration import Iteration
from ..parameters import Parameters
from ..utilities.basics import Array, Bounds, Error


class ProblemMarket(Market):
    """A market underlying the BLP problem."""

    def solve_demand(
            self, initial_delta: Array, parameters: Parameters, iteration: Iteration, fp_type: str,
            compute_jacobian: bool) -> Tuple[Array, Array, List[Error], bool, int, int]:
        """Compute the mean utility for this market that equates market shares to observed values by solving a fixed
        point problem. Then, if compute_jacobian is True, compute the Jacobian of xi (equivalently, of delta) with
        respect to theta. If necessary, replace null elements in delta with their last values before computing its
        Jacobian.
        """
        errors: List[Error] = []

        # configure NumPy to identify floating point errors
        with np.errstate(divide='call', over='call', under='ignore', invalid='call'):
            np.seterrcall(lambda *_: errors.append(exceptions.DeltaFloatingPointError()))

            # compute delta either with a closed-form solution or by solving a fixed point problem
            if self.K2 == 0:
                converged = True
                iterations = evaluations = 0
                outside_share = 1 - self.products.shares.sum()
                delta = np.log(self.products.shares) - np.log(outside_share)
                if self.H > 0:
                    group_shares = self.products.shares / self.groups.expand(self.groups.sum(self.products.shares))
                    delta -= self.rho * np.log(group_shares)
            elif fp_type in {'safe', 'linear'}:
                log_shares = np.log(self.products.shares)
                compute_probabilities = functools.partial(self.compute_probabilities, safe=fp_type == 'safe')
                if self.H == 0:
                    if not iteration._compute_jacobian:
                        def contraction(next_delta: Array) -> Union[Tuple[Array, Array], Array]:
                            """Compute the next linear delta."""
                            shares = compute_probabilities(next_delta)[0] @ self.agents.weights
                            return next_delta + log_shares - np.log(shares)
                    else:
                        # pre-compute a Jacobian component
                        eye = np.eye(self.J)

                        # define the contraction to also return the Jacobian
                        def contraction(next_delta: Array) -> Union[Tuple[Array, Array], Array]:
                            """Compute the next linear delta and its Jacobian."""
                            probabilities = compute_probabilities(next_delta)[0]
                            shares = probabilities @ self.agents.weights
                            weighted_probabilities = self.agents.weights * probabilities.T
                            return (
                                next_delta + log_shares - np.log(shares),
                                -eye + (probabilities @ weighted_probabilities) / shares
                            )
                else:
                    if not iteration._compute_jacobian:
                        def contraction(next_delta: Array) -> Union[Tuple[Array, Array], Array]:
                            """Compute the next linear delta under nesting."""
                            shares = compute_probabilities(next_delta)[0] @ self.agents.weights
                            return next_delta + (log_shares - np.log(shares)) * (1 - self.rho)
                    else:
                        # pre-compute Jacobian components
                        membership = self.get_membership_matrix()
                        weighted_eye = (1 + self.rho) * np.eye(self.J)

                        # define the contraction to also return the Jacobian
                        def contraction(next_delta: Array) -> Union[Tuple[Array, Array], Array]:
                            """Compute the next linear delta and its Jacobian under nesting."""
                            probabilities, conditionals = compute_probabilities(next_delta)
                            shares = probabilities @ self.agents.weights
                            weighted_probabilities = self.agents.weights * probabilities.T / shares.T
                            return (
                                next_delta + (log_shares - np.log(shares)) * (1 - self.rho),
                                -weighted_eye + probabilities @ weighted_probabilities + self.rho * membership * (
                                    conditionals @ weighted_probabilities
                                )
                            )

                # solve the linear contraction mapping
                delta, converged, iterations, evaluations = iteration._iterate(initial_delta, contraction)
            else:
                assert fp_type == 'nonlinear' and not iteration._compute_jacobian
                exp_mu = np.exp(self.mu)
                compute_probabilities = functools.partial(self.compute_probabilities, mu=exp_mu, linear=False)

                # define the nonlinear contraction mapping
                if self.H == 0:
                    def contraction(next_delta: Array) -> Union[Tuple[Array, Array], Array]:
                        """Compute the next exponentiated delta."""
                        quotient = compute_probabilities(next_delta, numerator=exp_mu)[0]
                        return self.products.shares / (quotient @ self.agents.weights)
                else:
                    def contraction(next_delta: Array) -> Union[Tuple[Array, Array], Array]:
                        """Compute the next exponentiated delta under nesting."""
                        shares = compute_probabilities(next_delta)[0] @ self.agents.weights
                        return next_delta * (self.products.shares / shares)**(1 - self.rho)

                # solve the nonlinear contraction mapping
                exp_delta, converged, iterations, evaluations = iteration._iterate(np.exp(initial_delta), contraction)
                delta = np.log(exp_delta)

        # check for convergence
        if not converged:
            errors.append(exceptions.DeltaConvergenceError())

        # if the gradient is to be computed, replace invalid values in delta with the last computed values before
        #   computing its Jacobian
        xi_jacobian = np.full((self.J, parameters.P), np.nan, options.dtype)
        if compute_jacobian:
            valid_delta = delta.copy()
            bad_delta_index = ~np.isfinite(delta)
            valid_delta[bad_delta_index] = initial_delta[bad_delta_index]
            xi_jacobian, jacobian_errors = self.compute_xi_by_theta_jacobian(parameters, valid_delta)
            errors.extend(jacobian_errors)
        return delta, xi_jacobian, errors, converged, iterations, evaluations

    def solve_supply(
            self, initial_tilde_costs: Array, xi_jacobian: Array, parameters: Parameters, costs_type: str,
            costs_bounds: Bounds, compute_jacobian: bool) -> Tuple[Array, Array, Array, List[Error]]:
        """Compute transformed marginal costs for this market. Then, if compute_jacobian is True, compute the Jacobian
        of omega (equivalently, of transformed marginal costs) with respect to theta. If necessary, replace null
        elements in transformed marginal costs with their last values before computing their Jacobian.
        """
        errors: List[Error] = []

        # configure NumPy to identify floating point errors
        with np.errstate(divide='call', over='call', under='ignore', invalid='call'):
            np.seterrcall(lambda *_: errors.append(exceptions.CostsFloatingPointError()))

            # compute marginal costs
            eta, eta_errors = self.compute_eta()
            errors.extend(eta_errors)
            costs = self.products.prices - eta

            # clip marginal costs that are outside of acceptable bounds
            clipped_costs = (costs < costs_bounds[0]) | (costs > costs_bounds[1])
            if clipped_costs.any():
                costs = np.clip(costs, *costs_bounds)

            # take the log of marginal costs under a log-linear specification
            if costs_type == 'linear':
                tilde_costs = costs
            else:
                assert costs_type == 'log'
                if np.any(costs <= 0):
                    errors.append(exceptions.NonpositiveCostsError())
                with np.errstate(all='ignore'):
                    tilde_costs = np.log(costs)

        # if the gradient is to be computed, replace invalid transformed marginal costs with their last computed
        #   values before computing their Jacobian, which is zero for clipped marginal costs
        omega_jacobian = np.full((self.J, parameters.P), np.nan, options.dtype)
        if compute_jacobian:
            valid_tilde_costs = tilde_costs.copy()
            bad_tilde_costs_index = ~np.isfinite(tilde_costs)
            valid_tilde_costs[bad_tilde_costs_index] = initial_tilde_costs[bad_tilde_costs_index]
            omega_jacobian, jacobian_errors = self.compute_omega_by_theta_jacobian(
                valid_tilde_costs, xi_jacobian, parameters, costs_type
            )
            errors.extend(jacobian_errors)
            omega_jacobian[clipped_costs.flat] = 0
        return tilde_costs, omega_jacobian, clipped_costs, errors
