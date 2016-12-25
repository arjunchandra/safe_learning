"""An efficient implementation of Delaunay triangulation on regular grids."""

from __future__ import absolute_import, print_function, division

from collections import Sequence

import numpy as np
from scipy import spatial, sparse
from sklearn.utils.extmath import cartesian


__all__ = ['DeterministicFunction', 'Triangulation', 'PiecewiseConstant',
           'GridWorld', 'PiecewiseConstant']


class Function(object):
    """A generic function class."""

    def __init__(self):
        super(Function, self).__init__()
        self.ndim = None


class UncertainFunction(Function):
    """Base class for function approximators."""

    def __init__(self):
        super(UncertainFunction, self).__init__()

    def evaluate(self, points, full_cov=True):
        """Return the distribution over function values.

        Parameters
        ----------
        points : ndarray
            The points at which to evaluate the function. One row for each
            data points.
        full_cov : bool
            Whether to return a full covariance matrix for each data point, or
            only the diagonal part.

        Returns
        -------
        mean : ndarray
            The expected function values at the points.
        var : ndarray
            The variance of the estimate.
        """
        raise NotImplementedError()

    def gradient(self, points):
        """Return the distribution over the gradient.

        Parameters
        ----------
        points : ndarray
            The points at which to evaluate the function. One row for each
            data points.
        full_cov : bool
            Whether to return a full covariance tensor for each data point, or
            only the diagonal part.

        Returns
        -------
        mean : ndarray
            The expected function gradient at the points.
        var : ndarray
            The variance of the estimate.
        """
        raise NotImplementedError()


class DeterministicFunction(Function):
    """Base class for function approximators."""

    def __init__(self):
        """Initialization, see `Function` for details."""
        super(DeterministicFunction, self).__init__()

    def evaluate(self, points):
        """Return the function values.

        Parameters
        ----------
        points : ndarray
            The points at which to evaluate the function. One row for each
            data points.

        Returns
        -------
        values : ndarray
            The function values at the points.
        """
        raise NotImplementedError()

    def gradient(self, points):
        """Return the gradient.

        Parameters
        ----------
        points : ndarray
            The points at which to evaluate the function. One row for each
            data points.
        Returns
        -------
        gradient : ndarray
            The function gradient at the points.
        """
        raise NotImplementedError()


class ScipyDelaunay(spatial.Delaunay):
    """
    A dummy triangulation on a regular grid, very inefficient.

    Warning: The internal indexing is different from the one used in our
    implementation!

    Parameters
    ----------
    limits: 2d array-like
        A list of limits. For example, [(x_min, x_max), (y_min, y_max)]
    num_points: 1d array-like
        The number of points with which to grid each dimension.
    """

    def __init__(self, limits, num_points):
        self.numpoints = num_points
        self.limits = np.asarray(limits, dtype=np.float)
        params = [np.linspace(limit[0], limit[1], n + 1) for limit, n in
                  zip(limits, num_points)]
        output = np.meshgrid(*params)
        points = np.array([par.ravel() for par in output]).T
        super(ScipyDelaunay, self).__init__(points)


class GridWorld(object):
    """Base class for function approximators on a regular grid.

    Parameters
    ----------
    limits: 2d array-like
        A list of limits. For example, [(x_min, x_max), (y_min, y_max)]
    num_points: 1d array-like
        The number of points with which to grid each dimension.
    """

    def __init__(self, limits, num_points, vertex_values=None):
        """Initialization, see `GridWorld`."""
        super(GridWorld, self).__init__()

        self.limits = np.asarray(limits, dtype=np.float)

        if not (isinstance(num_points, Sequence) or
                isinstance(num_points, np.ndarray)):
            num_points = [num_points] * len(limits)
        self.num_points = np.atleast_1d(np.asarray(num_points, dtype=np.int))

        # Compute offset and unit hyperrectangle
        self.offset = self.limits[:, 0]
        self.unit_maxes = (self.limits[:, 1] - self.offset) / self.num_points
        self.offset_limits = np.stack((np.zeros_like(self.limits[:, 0]),
                                       self.limits[:, 1] - self.offset),
                                      axis=1)

        # Statistics about the grid
        self.nrectangles = np.prod(self.num_points)
        self.nindex = np.prod(self.num_points + 1)
        self.ndim = len(limits)

        self.vertex_values = None
        if vertex_values is not None:
            self.vertex_values = np.asarray(vertex_values)

    def _center_states(self, states, clip=True):
        """Center the states to the interval [0, x].

        Parameters
        ----------
        states : np.array
        clip : bool, optinal
            If False the data is not clipped to lie within the limits.

        Returns
        -------
        offset_states : ndarray
        """
        states = np.atleast_2d(states) - self.offset[None, :]
        eps = np.finfo(states.dtype).eps
        if clip:
            np.clip(states,
                    self.offset_limits[:, 0] + 2 * eps,
                    self.offset_limits[:, 1] - 2 * eps,
                    out=states)
        return states

    def index_to_state(self, indices):
        """Convert indices to physical states.

        Parameters
        ----------
        indices : ndarray (int)
            The indices of points on the discretization.

        Returns
        -------
        states : ndarray
            The states with physical units that correspond to the indices.
        """
        indices = np.atleast_1d(indices)
        ijk_index = np.vstack(np.unravel_index(indices, self.num_points + 1)).T
        return ijk_index * self.unit_maxes + self.offset

    def state_to_index(self, states):
        """Convert physical states to indices.

        Parameters
        ----------
        states: ndarray
            Physical states on the discretization.

        Returns
        -------
        indices: ndarray (int)
            The indices that correspond to the physical states.
        """
        states = np.atleast_2d(states)
        states = np.clip(states, self.limits[:, 0], self.limits[:, 1])
        states = (states - self.offset) / self.unit_maxes
        ijk_index = np.rint(states).astype(np.int)
        return np.ravel_multi_index(ijk_index.T, self.num_points + 1)

    def state_to_rectangle(self, states, offset=True):
        """Convert physical states to its closest rectangle index.

        Parameters
        ----------
        states : ndarray
            Physical states on the discretization.
        offset : bool, optional
            If False the data is assumed to be already centered and clipped.

        Returns
        -------
        rectangles : ndarray (int)
            The indices that correspond to rectangles of the physical states.
        """
        # clip to domain (find closest rectangle)
        if offset:
            states = self._center_states(states, clip=True)

        ijk_index = np.floor_divide(states, self.unit_maxes).astype(np.int)
        return np.ravel_multi_index(ijk_index.T,
                                    self.num_points)

    def rectangle_to_state(self, rectangles):
        """
        Convert rectangle indices to the states of the bottem-left corners.

        Parameters
        ----------
        rectangles : ndarray (int)
            The indices of the rectangles

        Returns
        -------
        states : ndarray
            The states that correspond to the bottom-left corners of the
            corresponding rectangles.
        """
        rectangles = np.atleast_1d(rectangles)
        ijk_index = np.vstack(np.unravel_index(rectangles, self.num_points)).T
        return (ijk_index * self.unit_maxes) + self.offset

    def rectangle_corner_index(self, rectangles):
        """Return the index of the bottom-left corner of the rectangle.

        Parameters
        ----------
        rectangles: ndarray
            The indices of the rectangles.

        Returns
        -------
        corners : ndarray (int)
            The indices of the bottom-left corners of the rectangles.
        """
        ijk_index = np.vstack(np.unravel_index(rectangles, self.num_points)).T
        return np.ravel_multi_index(np.atleast_2d(ijk_index).T,
                                    self.num_points + 1)


class PiecewiseConstant(GridWorld, DeterministicFunction):
    """A piecewise constant function approximator.

    Parameters
    ----------
    limits: 2d array-like
        A list of limits. For example, [(x_min, x_max), (y_min, y_max)]
    num_points: 1d array-like
        The number of points with which to grid each dimension.
    vertex_values: 1d array_like, optional
        The values at the vertices of the grid.
    """

    def __init__(self, limits, num_points, vertex_values=None):
        """Initialization, see `PiecewiseConstant`."""
        super(PiecewiseConstant, self).__init__(limits, num_points,
                                                vertex_values=vertex_values)

    def evaluate(self, points):
        """Return the function values.

        Parameters
        ----------
        points : ndarray
            The points at which to evaluate the function. One row for each
            data points.

        Returns
        -------
        values : ndarray
            The function values at the points.
        """
        nodes = self.state_to_index(points)
        return self.vertex_values[nodes]

    def evaluate_constraint(self, points):
        """
        Obtain function values at points from triangulation.

        This function returns a sparse matrix that, when multiplied
        with the vector with all the function values on the vertices,
        returns the function values at points.

        Parameters
        ----------
        points : 2d array
            Each row represents one point

        Returns
        -------
        values
            A sparse matrix B so that evaluate(points) = B.dot(vertex_values).
        """
        npoints = len(points)
        weights = np.ones(npoints, dtype=np.int)
        rows = np.arange(npoints)
        cols = self.state_to_index(points)
        return sparse.coo_matrix((weights, (rows, cols)),
                                 shape=(npoints, self.nindex))

    def gradient(self, points):
        """Return the gradient.

        The gradient is always zero for piecewise constant functions!

        Parameters
        ----------
        points : ndarray
            The points at which to evaluate the function. One row for each
            data points.
        Returns
        -------
        gradient : ndarray
            The function gradient at the points.
        """
        return np.zeros((len(points), self.nindex))


class Triangulation(GridWorld, DeterministicFunction):
    """
    Efficient Delaunay triangulation on regular grids.

    This class is a wrapper around scipy.spatial.Delaunay for regular grids. It
    splits the space into regular hyperrectangles and then computes a Delaunay
    triangulation for only one of them. This single triangulation is then
    generalized to other hyperrectangles, without ever maintaining the full
    triangulation for all individual hyperrectangles.

    Parameters
    ----------
    limits: 2d array-like
        A list of limits. For example, [(x_min, x_max), (y_min, y_max)]
    num_points: 1d array-like
        The number of points with which to grid each dimension.
    vertex_values: 1d array_like, optional
        The values at the vertices of the grid.
    project: bool, optional
        Whether to project points onto the limits.
    """

    def __init__(self, limits, num_points, vertex_values=None, project=False):
        """Initialization."""
        super(Triangulation, self).__init__(limits, num_points,
                                            vertex_values=vertex_values)

        # Get triangulation
        hyperrectangle_corners = cartesian(np.diag(self.unit_maxes))
        self.triangulation = spatial.Delaunay(hyperrectangle_corners)
        self.unit_simplices = self._triangulation_simplex_indices()

        # Some statistics about the triangulation
        self.nsimplex = self.triangulation.nsimplex * self.nrectangles

        # Parameters for the hyperplanes of the triangulation
        self.hyperplanes = None
        self._update_hyperplanes()

        self.project = project

    def _triangulation_simplex_indices(self):
        """Return the simplex indices in our coordinates.

        Returns
        -------
        simplices: ndarray (int)
            The simplices array in our extended coordinate system.

        Notes
        -----
        This is only used once in the initialization.
        """
        simplices = self.triangulation.simplices
        new_simplices = np.empty_like(simplices)

        # Convert the points to out indices
        index_mapping = self.state_to_index(self.triangulation.points +
                                            self.offset)

        # Replace each index with out new_index in index_mapping
        for i, new_index in enumerate(index_mapping):
            new_simplices[simplices == i] = new_index
        return new_simplices

    def _update_hyperplanes(self):
        """Compute the simplex hyperplane parameters on the triangulation."""
        self.hyperplanes = np.empty((self.triangulation.nsimplex,
                                     self.ndim, self.ndim),
                                    dtype=np.float)

        # Use that the bottom-left rectangle has the index zero, so that the
        # index numbers of scipy correspond to ours.
        for i, simplex in enumerate(self.unit_simplices):
            simplex_points = self.index_to_state(simplex)
            self.hyperplanes[i] = np.linalg.inv(simplex_points[1:] -
                                                simplex_points[:1])

    def find_simplex(self, points):
        """Find the simplices corresponding to points.

        Parameters
        ----------
        points : 2darray

        Returns
        -------
        simplices : np.array (int)
            The indices of the simplices
        """
        points = self._center_states(points, clip=True)

        # Convert to basic hyperrectangle coordinates and find simplex
        unit_coordinates = points % self.unit_maxes
        simplex_ids = self.triangulation.find_simplex(unit_coordinates)

        # Adjust for the hyperrectangle index
        rectangles = self.state_to_rectangle(points, offset=False)
        simplex_ids += rectangles * self.triangulation.nsimplex

        return simplex_ids

    def simplices(self, indices):
        """Return the simplices corresponding to the simplex index.

        Parameters
        ----------
        indices : ndarray
            The indices of the simpleces

        Returns
        -------
        simplices : ndarray
            Each row consists of the indices of the simplex corners.
        """
        # Get the indices inside the unit rectangle
        unit_indices = np.remainder(indices, self.triangulation.nsimplex)
        simplices = self.unit_simplices[unit_indices].copy()

        # Shift indices to corresponding rectangle
        rectangles = np.floor_divide(indices, self.triangulation.nsimplex)
        corner_index = self.rectangle_corner_index(rectangles)

        if simplices.ndim > 1:
            corner_index = corner_index[:, None]

        simplices += corner_index
        return simplices

    def _get_weights(self, points):
        """Return the linear weights associated with points.

        Parameters
        ----------
        points : 2d array
            Each row represents one point

        Returns
        -------
        weights : ndarray
            An array that contains the linear weights for each point.
        simplices : ndarray
            The indeces of the simplices associated with each points
        """
        simplex_ids = self.find_simplex(points)

        if self.project:
            points = np.clip(points, self.limits[:, 0], self.limits[:, 1])

        simplices = self.simplices(simplex_ids)
        origins = self.index_to_state(simplices[:, 0])

        # Get hyperplane equations
        simplex_ids %= self.triangulation.nsimplex
        hyperplanes = self.hyperplanes[simplex_ids]

        # Some numbers for convenience
        nsimp = self.ndim + 1
        npoints = len(points)

        weights = np.empty((npoints, nsimp), dtype=np.float)

        # Pre-multiply each hyperplane by (point - origin)
        np.einsum('ij,ijk->ik', points - origins, hyperplanes,
                  out=weights[:, 1:])
        # The weights have to add up to one
        weights[:, 0] = 1 - np.sum(weights[:, 1:], axis=1)

        return weights, simplices

    def evaluate(self, points):
        """Return the function values.

        Parameters
        ----------
        points : ndarray
            The points at which to evaluate the function. One row for each
            data points.

        Returns
        -------
        values : ndarray
            The function values at the points.
        """
        weights, simplices = self._get_weights(points)
        # Return function values if desired
        return np.einsum('ij,ij->i', weights, self.vertex_values[simplices])

    def evaluate_constraint(self, points):
        """
        Obtain function values at points from triangulation.

        This function returns a sparse matrix that, when multiplied
        with the vector with all the function values on the vertices,
        returns the function values at points.

        Parameters
        ----------
        points : 2d array
            Each row represents one point.

        Returns
        -------
        values
            A sparse matrix B so that evaluate(points) = B.dot(vertex_values).
        """
        weights, simplices = self._get_weights(points)
        # Construct sparse matrix for optimization

        nsimp = self.ndim + 1
        npoints = len(simplices)
        # Indices of constraints (nsimp points per simplex, so we have nsimp
        #  values in each row; one for each simplex)
        rows = np.repeat(np.arange(len(points)), nsimp)
        cols = simplices.ravel()

        return sparse.coo_matrix((weights.ravel(), (rows, cols)),
                                 shape=(npoints, self.nindex))

    def _get_weights_gradient(self, points):
        """Return the linear gradient weights asscoiated with points.

        Parameters
        ----------
        points : 2d array
            Each row represents one point

        Returns
        -------
        weights : ndarray
            An array that contains the linear weights for each point.
        simplices : ndarray
            The indeces of the simplices associated with each points
        """
        simplex_ids = self.find_simplex(points)
        simplices = self.simplices(simplex_ids)

        # Get hyperplane equations
        simplex_ids %= self.triangulation.nsimplex

        # Some numbers for convenience
        nsimp = self.ndim + 1
        npoints = len(simplex_ids)

        # weights
        weights = np.empty((npoints, self.ndim, nsimp), dtype=np.float)

        weights[:, :, 1:] = self.hyperplanes[simplex_ids]
        weights[:, :, 0] = -np.sum(weights[:, :, 1:], axis=2)
        return weights, simplices

    def gradient(self, points):
        """Return the gradient.

        Parameters
        ----------
        points : ndarray
            The points at which to evaluate the function. One row for each
            data points.
        Returns
        -------
        gradient : ndarray
            The function gradient at the points.
        """
        weights, simplices = self._get_weights_gradient(points)
        # Return function values if desired
        return np.einsum('ijk,ik->ij', weights, self.vertex_values[simplices])

    def gradient_constraint(self, points):
        """
        Return the gradients at the respective points.

        This function returns a sparse matrix that, when multiplied
        with the vector of all the function values on the vertices,
        returns the gradients. Note that after the product you have to call
        ```np.reshape(grad, (ndim, -1))``` in order to obtain a proper
        gradient matrix.

        Parameters
        ----------
        points : 2d array
            Each row contains one state at which to evaluate the gradient.

        Returns
        -------
        gradient : scipy.sparse.coo_matrix
            A sparse matrix so that
            `grad(points) = B.dot(V(vertices)).reshape(ndim, -1)` corresponds
            to the true gradients
        """
        weights, simplices = self._get_weights_gradient(points)

        # Some numbers for convenience
        nsimp = self.ndim + 1
        npoints = len(simplices)

        # Construct sparse matrix for optimization

        # Indices of constraints (ndim gradients for each point, which each
        # depend on the nsimp vertices of the simplex.
        rows = np.repeat(np.arange(npoints * self.ndim), nsimp)
        cols = np.tile(simplices, (1, self.ndim)).ravel()

        return sparse.coo_matrix((weights.ravel(), (rows, cols)),
                                 shape=(self.ndim * npoints, self.nindex))