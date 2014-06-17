from __future__ import division

from collections import namedtuple, defaultdict, OrderedDict
from functools import partial
import itertools
import logging

from cyflann import FLANNIndex, FLANNParameters
import numpy as np
from scipy.special import gamma, gammaln, psi
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.externals.six import iteritems, itervalues
from sklearn.externals.six.moves import map, zip

from .. import Features
from ..utils import identity, ProgressLogger
from ._knn import _linear, kl, _alpha_div, _jensen_shannon_core

# TODO: cython version
from ._knn import _estimate_cross_divs


__all__ = ['KNNDivergenceEstimator']


################################################################################
### Logging setup
logger = logging.getLogger(__name__)

progress_logger = logging.getLogger(__name__ + '.progress')
progress_logger.propagate = False
progress_logger.addHandler(logging.NullHandler())

# TODO: break out into utils
def plog(it, name=None, total=None):
    return ProgressLogger(progress_logger, name=name)(it, total=total)


################################################################################
### Main class

class KNNDivergenceEstimator(BaseEstimator, TransformerMixin):
    '''

    Parameters
    ----------

    n_jobs : integer, optional
        The number of CPUs to use to do the computation. -1 means 'all CPUs'.
    '''
    def __init__(self, div_funcs=('kl',), Ks=(3,), do_sym=False, n_jobs=1,
                 clamp=True, min_dist=1e-3,
                 flann_algorithm='auto', flann_args=None):
        self.div_funcs = div_funcs
        self.Ks = Ks
        self.do_sym = do_sym
        self.n_jobs = n_jobs
        self.clamp = clamp
        self.min_dist = min_dist
        self.flann_algorithm = flann_algorithm
        self.flann_args = flann_args

        # check params, but need to re-run in fit() in case args are set by
        # a pipeline or whatever
        self._setup_args()

    def _setup_args(self):
        self.Ks = Ks = np.asarray(self.Ks)
        if Ks.ndim != 1:
            raise TypeError("Ks should be 1-dim, got shape {}".format(Ks.shape))
        if Ks.min() < 1:
            raise ValueError("Ks should be positive; got {}".format(Ks.min()))

        if not hasattr(self, 'funcs_base_'):
            self.funcs_base_, self.metas_base_, self.n_meta_only_ = \
                _parse_specs(self.div_funcs, Ks)

            self.save_all_Ks_ = any(getattr(f, 'needs_all_ks', False)
                                    for f in self.funcs_base_)

        # check flann args; just prevents a similar exception later
        try:
            FLANNParameters(**self._flann_args())
        except AttributeError as e:
            msg = "flann_args contains an invalid argument:\n  {}"
            raise TypeError(msg.format(e))

    @property
    def _n_jobs(self):
        if self.n_jobs == -1:
            from multiprocessing import cpu_count
            return cpu_count()
        return self.n_jobs

    def _flann_args(self, X=None):
        args = {'cores': self._n_jobs}
        if self.flann_algorithm == 'auto':
            if X is None or X.dim > 5:
                args['algorithm'] = 'linear'
            else:
                args['algorithm'] = 'kdtree_single'
        else:
            args['algorithm'] = self.flann_algorithm
        if self.flann_args:
            args.update(self.flann_args)
        return args

    def _choose_funcs(self, X, Y=None):
        self.funcs_, self.metas_ = _set_up_funcs(
            self.funcs_base_, self.metas_base_, self.Ks,
            X.dim, X.n_pts, None if Y is None else Y.n_pts)

        max_K = max(self.Ks.max(), getattr(self, 'max_K_', 0))
        for func in self.funcs_:
            if hasattr(func, 'k_needed'):
                max_K = max(max_K, func.k_needed)
        self.max_K_ = max_K

    def _check_features(self, X):
        # TODO: if we have the cython code, don't stack
        if isinstance(X, Features):
            X.make_stacked()
            return X.bare()
        else:
            return Features(X, stack=True)

    def _check_Ks(self, X, Y=None):
        min_pt = min(X.n_pts.min(), np.inf if Y is None else Y.n_pts.min())
        if self.max_K_ >= min_pt:
            msg = "asked for K = {}, but there's a bag with only {} points"
            raise ValueError(msg.format(self.max_K_, min_pt))

    def _build_indices(self, X):
        "Builds FLANN indices for each bag."
        # TODO: should probably multithread this
        logger.info("Building indices...")
        indices = [None] * len(X)
        for i, bag in enumerate(plog(X, name="index building")):
            indices[i] = idx = FLANNIndex(**self._flann_args())
            idx.build_index(bag)
        return indices

    def _get_rhos(self, X, indices):
        "Gets within-bag distances for each bag."
        logger.info("Getting within-bag distances...")

        # need to throw away the closest neighbor, which will always be self
        # thus K=1 corresponds to column 1 in the result array
        self._check_Ks(X)
        which_Ks = slice(1, None) if self.save_all_Ks_ else self.Ks
        min_dist = self.min_dist

        indices = plog(indices, name="within-bag distances")
        rhos = [None] * len(X)
        for i, (bag, idx) in enumerate(zip(X, indices)):
            r = np.sqrt(idx.nn_index(bag, self.max_K_ + 1)[1][:, which_Ks])
            np.maximum(min_dist, r, out=r)
            rhos[i] = r
        return rhos

    def _finalize(self, outputs, X_rhos, Y_rhos):
        if self.save_all_Ks_:
            X_rhos = [rho[:, self.Ks - 1] for rho in X_rhos]
            Y_rhos = [rho[:, self.Ks - 1] for rho in Y_rhos]

        for meta, info in iteritems(self.metas_):
            required = [outputs[[i]] for i in info.deps]
            args = ()
            if meta.needs_rhos[0]:
                args += (X_rhos,)
            if meta.needs_rhos[1]:
                args += (Y_rhos,)
            args += (required,)
            r = meta(*args, clamp=self.clamp)
            if r.ndim == 3:
                r = r[np.newaxis, :, :, :]
            outputs[info.pos, :, :, :] = r

        if not self.do_sym:
            outputs = outputs[:, :, :, :, 0]

        if self.n_meta_only_:
            outputs = np.ascontiguousarray(outputs[:-self.n_meta_only_])
        return outputs

    def fit(self, X, y=None, skip_rhos=False):
        '''
        Sets up for divergence estimation "from" X "to" new data. Builds
        FLANN indices and gets within-bag distances for X.
        '''
        self._setup_args()
        self.features_ = X = Features(X, bare=True)

        # if we're using a function that needs to pick its K vals itself,
        # then we need to set max_K here. when we transform(), might have to
        # re-do this :|
        self._choose_funcs(X)
        self._check_Ks(X)

        self.indices_ = self._build_indices(X)
        if not skip_rhos:
            self.rhos_ = self._get_rhos(X, self.indices_)
        return self

    def transform(self, X):
        Y = Features(X, bare=True)
        X = self.features_  # yes, naming here is confusing.
        # TODO: optimize for getting divergences among self

        old_max_K = self.max_K_
        self._choose_funcs(X, Y)
        self._check_Ks(X, Y)
        if hasattr(self, 'rhos_') and self.max_K_ > old_max_K:
            logger.warning(("Fit with a lower max_K ({}) than we actually need "
                            "({}); recomputing rhos. This should only happen "
                            "with Jensen-Shannon; if it's taking a significant "
                            "amount of time, pass skip_rhos=True to fit() or "
                            "set the max_K_ attribute to {} to avoid the "
                            "useless step.").format(
                                old_max_K, self.max_K_, self.max_K_))
            del self.rhos_

        if not hasattr(self, 'rhos_'):
            self.rhos_ = self._get_rhos(X, self.indices_)

        do_sym = self.do_sym or {
            req_pos for f, info in iteritems(self.metas_)
                    for req_pos, req in zip(info.deps, f.needs_results)
                    if req.needs_transpose}

        X_indices = self.indices_
        X_rhos = self.rhos_
        Y_indices = self._build_indices(Y)
        Y_rhos = self._get_rhos(Y, Y_indices) if do_sym else None

        logger.info("Getting divergences...")
        outputs = _estimate_cross_divs(
            X, X_indices, X_rhos, Y, Y_indices, Y_rhos,
            self.funcs_, self.Ks, self.max_K_, self.save_all_Ks_,
            len(self.div_funcs) + self.n_meta_only_, do_sym,
            partial(plog, name="Cross-divergences"),
            self._n_jobs, self.min_dist, self.clamp)
        logger.info("Getting meta functions...")
        outputs = self._finalize(outputs, X_rhos, Y_rhos)
        logger.info("Done with divergences.")
        return outputs


################################################################################
### Estimators of various divergences based on nearest-neighbor distances.
#
# The standard interface for these functions is:
#
# Function attributes:
#
#   needs_alpha: whether this function needs an alpha parameter. Default false.
#
#   self_value: The value that this function should take when comparing a
#               sample to itself: either a scalar constant or None (the
#               default), in which case the function is still called with
#               rhos = nus.
#
#   chooser_fn: a function that gets alphas (if needs_alpha), Ks, dim, X_ns, Y_ns
#               (the arrays of bag sizes) and returns a partial() of a "core"
#               function, with some things precomputed. If not present, just
#               does partial(fn, [alphas,] Ks, dim).
#
#   needs_all_ks: whether this function needs *all* the neighbor distances up
#                 to the max K value, rather than just the values of K that are
#                 actually used. Default false.
#
#   chooser_fn.returns_ks: whether the chooser_fn returns the max value of K
#                          needed. This allows an estimator function to require
#                          a higher value of K than requested by the user. Only
#                          if needs_all_ks; default false.
#
# Arguments:
#
#   alphas (if needs_alpha; array-like, scalar or 1d): the alpha values to use
#
#   Ks (array-like, scalar or 1d): the K values used
#
#   dim (scalar): the dimension of the feature space
#
#   num_q (scalar): the number of points in the sample from q
#
#   rhos: an array of within-bag nearest neighbor distances for a sample from p.
#         rhos[i, j] should be the distance from the ith sample from p to its
#         Ks[j]'th neighbor in the same sample. Shape: (num_p, num_Ks).
#   nus: an array of nearest neighbor distances from samples from other dists.
#        nus[i, j] should be the distance from the ith sample from p to its
#        Ks[j]'th neighbor in the sample from q. Shape: (num_p, num_Ks).
#
# Returns an array of divergence estimates. If needs_alpha, should be of shape
# (num_alphas, num_Ks); otherwise, of shape (num_Ks,).

def linear(Ks, dim, num_q, rhos, nus):
    r'''
    Estimates the linear inner product \int p q between two distributions,
    based on kNN distances.
    '''
    return _get_linear(Ks, dim)(num_q, rhos, nus)

def _get_linear(Ks, dim, X_ns=None, Y_ns=None):
    # Estimated with alpha=0, beta=1:
    #   B_{k,d,0,1} = (k - 1) / pi^(dim/2) * gamma(dim/2 + 1)
    #   (using gamma(k) / gamma(k - 1) = k - 1)
    Ks = np.reshape(Ks, (-1,))
    Bs = (Ks - 1) / np.pi ** (dim / 2) * gamma(dim / 2 + 1)  # shape (num_Ks,)
    return partial(_linear, Bs, dim)
linear.self_value = None  # have to execute it
linear.needs_alpha = False
linear.chooser_fn = _get_linear

# kl function is entirely in _np_divs (nothing to precompute)

def alpha_div(alphas, Ks, dim, num_q, rhos, nus):
    r'''
    Estimate the alpha divergence between distributions:
        \int p^\alpha q^(1-\alpha)
    based on kNN distances.

    Used in Renyi, Hellinger, Bhattacharyya, Tsallis divergences.

    Enforces that estimates are >= 0.

    Returns divergence estimates with shape (num_alphas, num_Ks).
    '''
    return _get_alpha_div(alphas, Ks, dim)(num_q, rhos, nus)

def _get_alpha_div(alphas, Ks, dim, X_ns=None, Y_ns=None):
    alphas = np.reshape(alphas, (-1, 1))
    Ks = np.reshape(Ks, (1, -1))

    omas = 1 - alphas

    # We're estimating with alpha = alpha-1, beta = 1-alpha.
    # B constant in front:
    #   estimator's alpha = -beta, so volume of unit ball cancels out
    #   and then ratio of gamma functions
    Bs = np.exp(gammaln(Ks) * 2 - gammaln(Ks + omas) - gammaln(Ks - omas))

    return partial(_alpha_div, omas, Bs, dim)

alpha_div.self_value = 1
alpha_div.needs_alpha = True
alpha_div.chooser_fn = _get_alpha_div


def jensen_shannon_core(Ks, dim, num_q, rhos, nus):
    r'''
    Estimates
          1/2 mean_X( d * log radius of largest ball in X+Y around X_i
                                with no more than M/(n+m-1) weight
                                where X points have weight 1 / (2 n - 1)
                                  and Y points have weight n / (m (2 n - 1))
                      - digamma(# of neighbors in that ball))

    This is the core pairwise component of the estimator of Jensen-Shannon
    divergence based on the Hino-Murata weighted information estimator. See
    the docstring for jensen_shannon for an explanation.
    '''
    ns = np.array([rhos.shape[0], num_q])
    return _get_jensen_shannon_core(Ks, dim, ns)[0](num_q, rhos, nus)

def _get_jensen_shannon_core(Ks, dim, X_ns, Y_ns):
    # precompute the max/min possible digamma(i) values: the floors/ceils of
    #
    #   M/(n+m-1) / (1 / (2 n - 1))
    #   M/(n+m-1) / (n / (m (2 n - 1)))
    #
    # for any valid value of n, m.

    min_X_n = np.min(X_ns)
    max_X_n = np.max(X_ns)
    if Y_ns is None:
        min_Y_n = min_X_n
        max_Y_n = max_X_n
    else:
        min_Y_n = np.min(Y_ns)
        max_Y_n = np.max(Y_ns)
    min_K = np.min(Ks)
    max_K = np.max(Ks)

    # figure out the smallest i value we might need (# of neighbors in ball)

    wt_bounds = [np.inf, -np.inf]
    min_wt_n = None; min_wt_m = None
    # max_wt_n = None; max_wt_m = None
    n_ms = list(itertools.product([min_X_n, max_X_n], [min_Y_n, max_Y_n]))
    for n, m in itertools.chain(n_ms, map(reversed, n_ms)):
        base = (2 * n - 1) / (n + m - 1)

        for wt in (base, base * m / n):
            if wt < wt_bounds[0]:
                wt_bounds[0] = wt
                min_wt_n = n
                min_wt_m = m

            if wt > wt_bounds[1]:
                wt_bounds[1] = wt
                # max_wt_n = n
                # max_wt_m = m

    if wt_bounds[0] * min_K < 1:
        msg = "K={} is too small for Jensen-Shannon estimator with n={}, m={}"
        raise ValueError((msg + "; must be at least {}").format(
             min_K, min_wt_n, min_wt_m, int(np.ceil(1 / wt_bounds[0]))))

    min_i = int(np.floor(wt_bounds[0] * min_K))
    max_i = int(np.ceil( wt_bounds[1] * max_K))
    digamma_vals = psi(np.arange(min_i, max_i + 1))

    # TODO: If we don't actually hit the worst case, might be nice to still
    #       run and just nan those elements that we can't compute. This is
    #       over-conservative.
    return partial(_jensen_shannon_core, Ks, dim, min_i, digamma_vals), max_i

jensen_shannon_core.needs_alpha = False
jensen_shannon_core.chooser_fn = _get_jensen_shannon_core
jensen_shannon_core.needs_all_ks = True
jensen_shannon_core.chooser_fn.returns_ks = True
jensen_shannon_core.self_value = np.nan
# The self_value should be the entropy estimate. But since we'll just subtract
# that later, don't bother computing it.


################################################################################
### Meta-estimators: things that need some additional computation on top of
###                  the per-bag stuff of the functions above.

# These functions are run after the base estimators above are complete.
#
# The interface here is:
#
# Function attributes:
#
#   needs_alpha: whether this function needs an alpha parameter. Default false.
#
#   needs_results: a list of MetaRequirement objects (below).
#                  Note that it is legal for meta estimators to depend on other
#                  meta estimators; circular dependencies cause the spec parser
#                  to crash.
#
# Arguments:
#
#   alphas (if needs_alpha; array-like, scalar or 1d): the alpha values to use
#
#   Ks (array-like, scalar or 1d): the K values used
#
#   dim (scalar): the dimension of the feature space
#
#   rhos: a list of within-bag NN distances, each of which is like the rhos
#         argument above
#
#   required: a list of the results array for each MetaRequirement classes,
#             each of shape (num_Ks, n_X, n_Y, 1 or 2),
#             where the last dimension depends on whether we're doing the
#             symmetric or not.
#
# Returns: array of results.
# If needs_alpha, has shape (num_alphas, num_Ks, n_X, n_Y, 1 or 2);
# otherwise, has shape (num_Ks, n_X, n_Y, 1 or 2)

MetaRequirement = namedtuple('MetaRequirement', 'func alpha needs_transpose')
# func: the function of the regular divergence that's needed
# alpha: None if no alpha is needed. Otherwise, can be a scalar alpha value,
#        or a callable which takes the (scalar or list) alphas for the meta
#        function and returns the required function's alpha(s).
# needs_transpose: if true, ensures the required results have both directions


def bhattacharyya(Ks, dim, required, clamp=True):
    r'''
    Estimate the Bhattacharyya coefficient between distributions, based on kNN
    distances:  \int \sqrt{p q}

    If clamp (the default), enforces 0 <= BC <= 1.

    Returns an array of shape (num_Ks,).
    '''
    est, = required
    if clamp:
        est = np.minimum(est, 1)  # BC <= 1
    return est
bhattacharyya.needs_alpha = False
bhattacharyya.needs_rhos = (False, False)
bhattacharyya.needs_results = [MetaRequirement(alpha_div, 0.5, False)]


def hellinger(Ks, dim, required, clamp=True):
    r'''
    Estimate the Hellinger distance between distributions, based on kNN
    distances:  \sqrt{1 - \int \sqrt{p q}}

    If clamp (the default), enforces 0 <= H <= 1.

    Returns a vector: one element for each K.
    '''
    bc, = required
    est = 1 - bc
    if clamp:
        np.maximum(est, 0, out=est)
        np.sqrt(est, out=est)
    return est
hellinger.needs_alpha = False
hellinger.needs_rhos = (False, False)
hellinger.needs_results = [MetaRequirement(alpha_div, 0.5, False)]


def renyi(alphas, Ks, dim, required, min_val=np.spacing(1), clamp=True):
    r'''
    Estimate the Renyi-alpha divergence between distributions, based on kNN
    distances:  1/(\alpha-1) \log \int p^alpha q^(1-\alpha)

    If the inner integral is less than min_val (default ``np.spacing(1)``),
    uses the log of min_val instead.

    If clamp (the default), enforces that the estimates are nonnegative by
    replacing any negative estimates with 0.

    Returns an array of shape (num_alphas, num_Ks).
    '''
    alphas = np.reshape(alphas, (-1, 1))
    est, = required

    est = np.maximum(est, min_val)  # TODO: can we modify in-place?
    np.log(est, out=est)
    est /= alphas - 1
    if clamp:
        np.maximum(est, 0, out=est)
    return est
renyi.needs_alpha = True
renyi.needs_rhos = (False, False)
renyi.needs_results = [MetaRequirement(alpha_div, identity, False)]


def tsallis(alphas, Ks, dim, required, clamp=True):
    r'''
    Estimate the Tsallis-alpha divergence between distributions, based on kNN
    distances:  (\int p^alpha q^(1-\alpha) - 1) / (\alpha - 1)

    If clamp (the default), enforces the estimate is nonnegative.

    Returns an array of shape (num_alphas, num_Ks).
    '''
    alphas = np.reshape(alphas, (-1, 1))
    alpha_est = required

    est = alpha_est - 1
    est /= alphas - 1
    if clamp:
        np.maximum(est, 0, out=est)
    return est
tsallis.needs_alpha = True
tsallis.needs_rhos = (False, False)
tsallis.needs_results = [MetaRequirement(alpha_div, identity, False)]


def l2(Ks, dim, X_rhos, Y_rhos, required, clamp=True):
    r'''
    Estimates the L2 distance between distributions, via
        \int (p - q)^2 = \int p^2 - \int p q - \int q p + \int q^2.

    \int pq and \int qp are estimated with the linear function (in both
    directions), while \int p^2 and \int q^2 are estimated via the quadratic
    function below.

    Always clamps negative estimates of l2^2 to 0, because otherwise the sqrt
    would break.
    '''
    n_X = len(X_rhos)
    n_Y = len(Y_rhos)

    linears, = required
    assert linears.shape == (1, Ks.size, n_X, n_Y, 2)

    X_quadratics = np.empty((n_X, Ks.size), dtype=np.float32)
    for i, rho in enumerate(X_rhos):
        X_quadratics[i, :] = quadratic(Ks, dim, rho)

    Y_quadratics = np.empty((n_Y, Ks.size), dtype=np.float32)
    for i, rho in enumerate(Y_rhos):
        Y_quadratics[i, :] = quadratic(Ks, dim, rho)

    est = -linears.sum(axis=4)
    est += X_quadratics.reshape(1, Ks.size, n_X, 1)
    est += Y_quadratics.reshape(1, Ks.size, 1, n_Y)
    np.maximum(est, 0, out=est)
    np.sqrt(est, out=est)

    # # diagonal is of course known to be zero
    # all_bags = xrange(n_bags)
    # est[all_bags, all_bags, :, :] = 0
    return est[:, :, :, :, None]
l2.needs_alpha = False
l2.needs_rhos = (True, True)
l2.needs_results = [MetaRequirement(linear, alpha=None, needs_transpose=True)]


# Not actually a meta-estimator, though it could be if it just repeated the
# values across rows (or columns).
def quadratic(Ks, dim, rhos, required=None):
    r'''
    Estimates \int p^2 based on kNN distances.

    In here because it's used in the l2 distance, above.

    Returns array of shape (num_Ks,).
    '''
    # Estimated with alpha=1, beta=0:
    #   B_{k,d,1,0} is the same as B_{k,d,0,1} in linear()
    # and the full estimator is
    #   B / (n - 1) * mean(rho ^ -dim)
    N = rhos.shape[0]
    Ks = np.asarray(Ks)
    Bs = (Ks - 1) / np.pi ** (dim / 2) * gamma(dim / 2 + 1)  # shape (num_Ks,)
    est = Bs / (N - 1) * np.mean(rhos ** (-dim), axis=0)
    return est


def jensen_shannon(Ks, dim, X_rhos, Y_rhos, required, clamp=True):
    r'''
    Estimate the difference between the Shannon entropy of an equally-weighted
    mixture between X and Y and the mixture of the Shannon entropies:

        JS(X, Y) = H[ (X + Y) / 2 ] - (H[X] + H[Y]) / 2

    We use a special case of the Hino-Murata weighted information estimator with
    a fixed M = n \alpha, about equivalent to the K-nearest-neighbor approach
    used for the other estimators:

        Hideitsu Hino and Noboru Murata (2013).
        Information estimators for weighted observations. Neural Networks.
        http://linkinghub.elsevier.com/retrieve/pii/S0893608013001676


    The estimator for JS(X, Y) is:

        log volume of the unit ball - log M + log(n + m - 1) + digamma(M)
        + 1/2 mean_X( d * log radius of largest ball in X+Y around X_i
                                with no more than M/(n+m-1) weight
                                where X points have weight 1 / (2 n - 1)
                                  and Y points have weight n / (m (2 n - 1))
                      - digamma(# of neighbors in that ball) )
        + 1/2 mean_Y( d * log radius of largest ball in X+Y around Y_i
                                with no more than M/(n+m-1) weight
                                where X points have weight m / (n (2 m - 1))
                                  and Y points have weight 1 / (2 m - 1)
                      - digamma(# of neighbors in that ball) )

        - 1/2 (log volume of the unit ball - log M + log(n - 1) + digamma(M))
        - 1/2 mean_X( d * log radius of the largest ball in X around X_i
                                with no more than M/(n-1) weight
                                where X points have weight 1 / (n - 1))
                      - digamma(# of neighbors in that ball) )

        - 1/2 (log volume of the unit ball - log M + log(m - 1) + digamma(M))
        - 1/2 mean_Y( d * log radius of the largest ball in Y around Y_i
                                with no more than M/(n-1) weight
                                where X points have weight 1 / (m - 1))
                      - digamma(# of neighbors in that ball) )

        =

        log(n + m - 1) + digamma(M)
        + 1/2 mean_X( d * log radius of largest ball in X+Y around X_i
                                with no more than M/(n+m-1) weight
                                where X points have weight 1 / (2 n - 1)
                                  and Y points have weight n / (m (2 n - 1))
                      - digamma(# of neighbors in that ball) )
        + 1/2 mean_Y( d * log radius of largest ball in X+Y around Y_i
                                with no more than M/(n+m-1) weight
                                where X points have weight m / (n (2 m - 1))
                                  and Y points have weight 1 / (2 m - 1)
                      - digamma(# of neighbors in that ball) )
        - 1/2 [log(n-1) + mean_X( d * log rho_M(X_i) )]
        - 1/2 [log(m-1) + mean_Y( d * log rho_M(Y_i) )]
    '''

    X_ns = np.array([rho.shape[0] for rho in X_rhos])
    Y_ns = np.array([rho.shape[0] for rho in Y_rhos])
    n_X = X_ns.size
    n_Y = Y_ns.size

    # cores[0, k, i, j, 0] is mean_X(d * ... - psi(...)) for X[i], Y[j], M=Ks[k]
    # cores[0, k, i, j, 1] is mean_Y(d * ... - psi(...)) for X[i], Y[j], M=Ks[k]
    cores, = required
    assert cores.shape == (1, Ks.size, n_X, n_Y, 2)

    # X_bits[k, i] is log(n-1) + mean_X( d * log rho_M(X_i) )  for X[i], M=Ks[k]
    X_bits = np.empty((Ks.size, n_X), dtype=np.float32)
    for i, rho in enumerate(X_rhos):
        X_bits[:, i] = dim * np.mean(np.log(rho), axis=0)
    X_bits += np.log(X_ns - 1)[np.newaxis, :]

    # Y_bits[k, j] is log(n-1) + mean_Y( d * log rho_M(Y_i) )  for Y[j], M=Ks[k]
    Y_bits = np.empty((Ks.size, n_Y), dtype=np.float32)
    for j, rho in enumerate(Y_rhos):
        Y_bits[:, j] = dim * np.mean(np.log(rho), axis=0)
    Y_bits += np.log(Y_ns - 1)[np.newaxis, :]

    est = cores.sum(axis=4)
    est -= X_bits.reshape(1, Ks.size, n_X, 1)
    est -= Y_bits.reshape(1, Ks.size, 1, n_Y)
    est /= 2
    est += np.log(-1 + X_ns[None, None, :, None] + Y_ns[None, None, None, :])
    est += psi(Ks)[None, :, None, None]

    # # diagonal is zero
    # all_bags = xrange(n_bags)
    # est[all_bags, all_bags, :, :] = 0

    if clamp:  # know that 0 <= JS <= ln(2)
        np.maximum(0, est, out=est)
        np.minimum(np.log(2), est, out=est)
    return est[:, :, :, :, None]
jensen_shannon.needs_alpha = False
jensen_shannon.needs_rhos = (True, True)
jensen_shannon.needs_results = [
    MetaRequirement(jensen_shannon_core, alpha=None, needs_transpose=True)]


################################################################################
### Parse string specifications into functions to use

func_mapping = {
    'linear': linear,
    'kl': kl,
    'alpha': alpha_div,
    'bc': bhattacharyya,
    'hellinger': hellinger,
    'renyi': renyi,
    'tsallis': tsallis,
    'l2': l2,
    'js-core': jensen_shannon_core,
    'js': jensen_shannon,
    'jensen-shannon': jensen_shannon,
}


def topological_sort(deps):
    '''
    Topologically sort a DAG, represented by a dict of child => set of parents.
    The dependency dict is destroyed during operation.

    Uses the Kahn algorithm: http://en.wikipedia.org/wiki/Topological_sorting
    Not a particularly good implementation, but we're just running it on tiny
    graphs.
    '''
    order = []
    available = set()

    def _move_available():
        to_delete = []
        for n, parents in iteritems(deps):
            if not parents:
                available.add(n)
                to_delete.append(n)
        for n in to_delete:
            del deps[n]

    _move_available()
    while available:
        n = available.pop()
        order.append(n)
        for parents in itervalues(deps):
            parents.discard(n)
        _move_available()

    if available:
        raise ValueError("dependency cycle found")
    return order


_FuncInfo = namedtuple('_FuncInfo', 'alphas pos')
_MetaFuncInfo = namedtuple('_MetaFuncInfo', 'alphas pos deps')
def _parse_specs(specs, Ks):
    '''
    Set up the different functions we need to call.

    Returns:
        - a dict mapping base estimator functions to _FuncInfo objects.
          If the function needs_alpha, then the alphas attribute is an array
          of alpha values and pos is a corresponding array of indices.
          Otherwise, alphas is None and pos is a list containing a single index.
          Indices are >= 0 if they correspond to something in a spec,
          and negative if they're just used for a meta estimator but not
          directly requested.
        - an OrderedDict mapping functions to _MetaFuncInfo objects.
          alphas and pos are like for _FuncInfo; deps is a list of indices
          which should be passed to the estimator. Note that these might be
          other meta functions; this list is guaranteed to be in an order
          such that all dependencies are resolved before calling that function.
          If no such order is possible, raise ValueError.
        - the number of meta-only results

    # TODO: update doctests for _parse_specs

    >>> _parse_specs(['renyi:.8', 'hellinger', 'renyi:.9'])
    ({<function alpha_div at 0x10954f848>:
            _FuncInfo(alphas=[0.8, 0.5, 0.9], pos=[-1, -2, -3])},
     OrderedDict([
        (<function hellinger at 0x10954fc80>,
            _MetaFuncInfo(alphas=None, pos=[1], deps=[array(-2)])),
        (<function renyi at 0x10954fcf8>,
            _MetaFuncInfo(alphas=[0.8, 0.9], pos=[0, 2], deps=[-1, -3]))
     ]), 3)

    >>> _parse_specs(['renyi:.8', 'hellinger', 'renyi:.9', 'l2'])
    ({<function alpha_div at 0x10954f848>:
        _FuncInfo(alphas=[0.8, 0.5, 0.9], pos=[-1, -2, -3]),
      <function linear at 0x10954f758>: _FuncInfo(alphas=None, pos=[-4])
     }, OrderedDict([
        (<function hellinger at 0x10954fc80>,
            _MetaFuncInfo(alphas=None, pos=[1], deps=[array(-2)])),
        (<function l2 at 0x10954fde8>,
            _MetaFuncInfo(alphas=None, pos=[3], deps=[-4])),
        (<function renyi at 0x10954fcf8>,
            _MetaFuncInfo(alphas=[0.8, 0.9], pos=[0, 2], deps=[-1, -3]))
     ]), 4)

    >>> _parse_specs(['renyi:.8', 'hellinger', 'renyi:.9', 'l2', 'linear'])
    ({<function alpha_div at 0x10954f848>:
        _FuncInfo(alphas=[0.8, 0.5, 0.9], pos=[-1, -2, -3]),
      <function linear at 0x10954f758>: _FuncInfo(alphas=None, pos=[4])
     }, OrderedDict([
        (<function hellinger at 0x10954fc80>,
            _MetaFuncInfo(alphas=None, pos=[1], deps=[array(-2)])),
        (<function l2 at 0x10954fde8>,
            _MetaFuncInfo(alphas=None, pos=[3], deps=[4])),
        (<function renyi at 0x10954fcf8>,
            _MetaFuncInfo(alphas=[0.8, 0.9], pos=[0, 2], deps=[-1, -3]))
     ]), 3)
    '''
    funcs = {}
    metas = {}
    meta_deps = defaultdict(set)

    def add_func(func, alpha=None, pos=None):
        needs_alpha = getattr(func, 'needs_alpha', False)
        is_meta = hasattr(func, 'needs_results')

        d = metas if is_meta else funcs
        if func not in d:
            if needs_alpha:
                args = {'alphas': [alpha], 'pos': [pos]}
            else:
                args = {'alphas': None, 'pos': [pos]}

            if not is_meta:
                d[func] = _FuncInfo(**args)
            else:
                d[func] = _MetaFuncInfo(deps=[], **args)
                for req in func.needs_results:
                    if callable(req.alpha):
                        req_alpha = req.alpha(alpha)
                    else:
                        req_alpha = req.alpha
                    add_func(req.func, alpha=req_alpha)
                    meta_deps[func].add(req.func)
                    meta_deps[req.func]  # make sure required func is in there

        else:
            # already have an entry for the func
            # need to give it this pos, if it's not None
            # and also make sure that the alpha is present
            info = d[func]
            if not needs_alpha:
                if pos is not None:
                    if info.pos != [None]:
                        msg = "{} passed more than once"
                        raise ValueError(msg.format(func_name))

                    info.pos[0] = pos
            else:  # needs alpha
                try:
                    idx = info.alphas.index(alpha)
                except ValueError:
                    # this is a new alpha value we haven't seen yet
                    info.alphas.append(alpha)
                    info.pos.append(pos)
                    if is_meta:
                        for req in func.needs_results:
                            if callable(req.alpha):
                                req_alpha = req.alpha(alpha)
                            else:
                                req_alpha = req.alpha
                            add_func(req.func, alpha=req_alpha)
                else:
                    # repeated alpha value
                    if pos is not None:
                        if info.pos[idx] is not None:
                            msg = "{} with alpha {} passed more than once"
                            raise ValueError(msg.format(func_name, alpha))
                        info.pos[idx] = pos

    # add functions for each spec
    for i, spec in enumerate(specs):
        func_name, alpha = (spec.split(':', 1) + [None])[:2]
        if alpha is not None:
            alpha = float(alpha)

        try:
            func = func_mapping[func_name]
        except KeyError:
            msg = "'{}' is not a known function type"
            raise ValueError(msg.format(func_name))

        needs_alpha = getattr(func, 'needs_alpha', False)
        if needs_alpha and alpha is None:
            msg = "{} needs alpha but not passed in spec '{}'"
            raise ValueError(msg.format(func_name, spec))
        elif not needs_alpha and alpha is not None:
            msg = "{} doesn't need alpha but is passed in spec '{}'"
            raise ValueError(msg.format(func_name, spec))

        add_func(func, alpha, i)

    # number things that are dependencies only
    meta_counter = itertools.count(-1, step=-1)
    for info in itertools.chain(itervalues(funcs), itervalues(metas)):
        for i, pos in enumerate(info.pos):
            if pos is None:
                info.pos[i] = next(meta_counter)

    # fill in the dependencies for metas
    for func, info in iteritems(metas):
        deps = info.deps
        assert deps == []

        for req in func.needs_results:
            f = req.func
            req_info = (metas if hasattr(f, 'needs_results') else funcs)[f]
            if req.alpha is not None:
                if callable(req.alpha):
                    req_alpha = req.alpha(info.alphas)
                else:
                    req_alpha = req.alpha

                find_alpha = np.vectorize(req_info.alphas.index, otypes=[int])
                pos = np.asarray(req_info.pos)[find_alpha(req_alpha)]
                if np.isscalar(pos):
                    deps.append(pos[()])
                else:
                    deps.extend(pos)
            else:
                pos, = req_info.pos
                deps.append(pos)

    # topological sort of metas
    meta_order = topological_sort(meta_deps)
    metas_ordered = OrderedDict(
        (f, metas[f]) for f in meta_order if hasattr(f, 'needs_results'))

    return funcs, metas_ordered, -next(meta_counter) - 1


def _set_up_funcs(funcs, metas_ordered, Ks, dim, X_ns=None, Y_ns=None):
    # replace functions with partials of args
    def replace_func(func, info):
        needs_alpha = getattr(func, 'needs_alpha', False)

        new = None
        args = (Ks, dim)
        if needs_alpha:
            args = (info.alphas,) + args

        if hasattr(func, 'chooser_fn'):
            args += (X_ns, Y_ns)
            if (getattr(func, 'needs_all_ks', False) and
                    getattr(func.chooser_fn, 'returns_ks', False)):
                new, k = func.chooser_fn(*args)
                new.k_needed = k
            else:
                new = func.chooser_fn(*args)
        else:
            new = partial(func, *args)

        for attr in dir(func):
            if not (attr.startswith('__') or attr.startswith('func_')):
                setattr(new, attr, getattr(func, attr))
        return new

    rep_funcs = dict(
        (replace_func(f, info), info) for f, info in iteritems(funcs))
    rep_metas_ordered = OrderedDict(
        (replace_func(f, info), info) for f, info in iteritems(metas_ordered))

    return rep_funcs, rep_metas_ordered
