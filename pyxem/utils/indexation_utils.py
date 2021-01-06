# -*- coding: utf-8 -*-
# Copyright 2016-2020 The pyXem developers
#
# This file is part of pyXem.
#
# pyXem is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyXem is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyXem.  If not, see <http://www.gnu.org/licenses/>.


from itertools import combinations
from operator import attrgetter

import numpy as np

from pyxem.utils.expt_utils import _cart2polar
from pyxem.utils.vector_utils import get_rotation_matrix_between_vectors
from pyxem.utils.vector_utils import get_angle_cartesian

from transforms3d.euler import mat2euler

from collections import namedtuple


# container for OrientationResults
OrientationResult = namedtuple(
    "OrientationResult",
    "phase_index rotation_matrix match_rate error_hkls total_error scale center_x center_y".split(),
)


def optimal_fft_size(target, real=False):
    """Wrapper around scipy function next_fast_len() for calculating optimal FFT padding.

    scipy.fft was only added in 1.4.0, so we fall back to scipy.fftpack
    if it is not available. The main difference is that next_fast_len()
    does not take a second argument in the older implementation.

    Parameters
    ----------
    target : int
        Length to start searching from. Must be a positive integer.
    real : bool, optional
        True if the FFT involves real input or output, only available
        for scipy > 1.4.0

    Returns
    -------
    int
        Optimal FFT size.
    """

    try:  # pragma: no cover
        from scipy.fft import next_fast_len

        support_real = True

    except ImportError:  # pragma: no cover
        from scipy.fftpack import next_fast_len

        support_real = False

    if support_real:  # pragma: no cover
        return next_fast_len(target, real)
    else:  # pragma: no cover
        return next_fast_len(target)


# Functions used in correlate_library.
def fast_correlation(image_intensities, int_local, pn_local, **kwargs):
    r"""Computes the correlation score between an image and a template

    Uses the formula

    .. math:: FastCorrelation
        \\frac{\\sum_{j=1}^m P(x_j, y_j) T(x_j, y_j)}{\\sqrt{\\sum_{j=1}^m T^2(x_j, y_j)}}

    Parameters
    ----------
    image_intensities: list
        list of intensity values in the image, for pixels where the template has a non-zero intensity
     int_local: list
        list of all non-zero intensities in the template
     pn_local: float
        pattern norm of the template

    Returns
    -------
    corr_local: float
        correlation score between template and image.

    See Also
    --------
    correlate_library, zero_mean_normalized_correlation

    """
    return (
        np.sum(np.multiply(image_intensities, int_local)) / pn_local
    )  # Correlation is the partially normalized dot product


def zero_mean_normalized_correlation(
    nb_pixels,
    image_std,
    average_image_intensity,
    image_intensities,
    int_local,
    **kwargs
):
    r"""Computes the correlation score between an image and a template.

    Uses the formula

    .. math:: zero_mean_normalized_correlation
        \\frac{\\sum_{j=1}^m P(x_j, y_j) T(x_j, y_j)- avg(P)avg(T)}{\\sqrt{\\sum_{j=1}^m (T(x_j, y_j)-avg(T))^2+\\sum_{Not {j}} avg(T)}}
        for a template T and an experimental pattern P.

    Parameters
    ----------
    nb_pixels: int
        total number of pixels in the image
    image_std: float
        Standard deviation of intensities in the image.
    average_image_intensity: float
        average intensity for the image
    image_intensities: list
        list of intensity values in the image, for pixels where the template has a non-zero intensity
     int_local: list
        list of all non-zero intensities in the template
     pn_local: float
        pattern norm of the template

    Returns
    -------
    corr_local: float
        correlation score between template and image.

    See Also
    --------
    correlate_library, fast_correlation

    """

    nb_pixels_star = len(int_local)
    average_pattern_intensity = nb_pixels_star * np.average(int_local) / nb_pixels

    match_numerator = (
        np.sum(np.multiply(image_intensities, int_local))
        - nb_pixels * average_image_intensity * average_pattern_intensity
    )
    match_denominator = image_std * (
        np.linalg.norm(int_local - average_pattern_intensity)
        + (nb_pixels - nb_pixels_star) * pow(average_pattern_intensity, 2)
    )

    if match_denominator == 0:
        corr_local = 0
    else:
        corr_local = (
            match_numerator / match_denominator
        )  # Correlation is the normalized dot product

    return corr_local


def full_frame_correlation(image_FT, image_norm, pattern_FT, pattern_norm):
    """Computes the correlation score between an image and a template in Fourier space.

    Parameters
    ----------
    image: numpy.ndarray
        Intensities of the image in fourier space, stored in a NxM numpy array
    image_norm: float
        The norm of the real space image, corresponding to image_FT
    fsize: numpy.ndarray
        The size of image_FT, for us in transform of template.
    template_coordinates: numpy array
        Array containing coordinates for non-zero intensities in the template
    template_intensities: list
        List of intensity values for the template.

    Returns
    -------
    corr_local: float
        Correlation score between image and template.

    See Also
    --------
    correlate_library, fast_correlation, zero_mean_normalized_correlation

    References
    ----------
    A. Foden, D. M. Collins, A. J. Wilkinson and T. B. Britton "Indexing electron backscatter diffraction patterns with
     a refined template matching approach" doi: https://doi.org/10.1016/j.ultramic.2019.112845
    """

    fprod = pattern_FT * image_FT

    res_matrix = np.fft.ifftn(fprod)
    fsize = res_matrix.shape
    corr_local = np.max(
        np.real(
            res_matrix[
                max(fsize[0] // 2 - 3, 0) : min(fsize[0] // 2 + 3, fsize[0]),
                max(fsize[1] // 2 - 3, 0) : min(fsize[1] // 2 + 3, fsize[1]),
            ]
        )
    )
    if image_norm > 0 and pattern_norm > 0:
        corr_local = corr_local / (image_norm * pattern_norm)

    # Sub-pixel refinement can be done here - Equation (5) in reference article

    return corr_local


def correlate_library_from_dict(image, template_dict, n_largest, method, mask):
    """Correlates all simulated diffraction templates in a DiffractionLibrary
    with a particular experimental diffraction pattern (image).

    Parameters
    ----------
    image : numpy.array
        The experimental diffraction pattern of interest.
    template_dict : dict
        Dictionary containing orientations, fourier transform of templates and template norms for
        every phase.
    n_largest : int
        The number of well correlated simulations to be retained.
    method : str
        Name of method used to compute correlation between templates and diffraction patterns. Can be
         'full_frame_correlation'. (I believe angular decomposition can also fit this framework)
    mask : bool
        A mask for navigation axes. 1 indicates positions to be indexed.


    Returns
    -------
    top_matches : numpy.array
        Array of shape (<num phases>*n_largest, 3) containing the top n
        correlated simulations for the experimental pattern of interest, where
        each entry is on the form [phase index, [z, x, z], correlation].


    References
    ----------
    full_frame_correlation:
    A. Foden, D. M. Collins, A. J. Wilkinson and T. B. Britton "Indexing electron backscatter diffraction patterns with
     a refined template matching approach" doi: https://doi.org/10.1016/j.ultramic.2019.112845
    """

    top_matches = np.empty((len(template_dict), n_largest, 3), dtype="object")

    if method == "full_frame_correlation":
        size = 2 * np.array(image.shape) - 1
        fsize = [optimal_fft_size(a, real=True) for a in (size)]
        image_FT = np.fft.fftshift(np.fft.rfftn(image, fsize))
        image_norm = np.sqrt(full_frame_correlation(image_FT, 1, image_FT, 1))

    if mask == 1:
        for phase_index, library_entry in enumerate(template_dict.values()):
            orientations = library_entry["orientations"]
            patterns = library_entry["patterns"]
            pattern_norms = library_entry["pattern_norms"]

            zip_for_locals = zip(orientations, patterns, pattern_norms)

            or_saved, corr_saved = np.empty((n_largest, 3)), np.zeros((n_largest, 1))

            for (or_local, pat_local, pn_local) in zip_for_locals:

                if method == "full_frame_correlation":
                    corr_local = full_frame_correlation(
                        image_FT, image_norm, pat_local, pn_local
                    )

                if corr_local > np.min(corr_saved):
                    or_saved[np.argmin(corr_saved)] = or_local
                    corr_saved[np.argmin(corr_saved)] = corr_local

                combined_array = np.hstack((or_saved, corr_saved))
                combined_array = combined_array[
                    np.flip(combined_array[:, 3].argsort())
                ]  # see stackoverflow/2828059 for details
                top_matches[phase_index, :, 0] = phase_index
                top_matches[phase_index, :, 2] = combined_array[:, 3]  # correlation
                for i in np.arange(n_largest):
                    top_matches[phase_index, i, 1] = combined_array[
                        i, :3
                    ]  # orientation

    return top_matches.reshape(-1, 3)


def correlate_library(image, library, n_largest, method, mask):
    r"""Correlates all simulated diffraction templates in a DiffractionLibrary
    with a particular experimental diffraction pattern (image).

    Calculated using the normalised (see return type documentation) dot
    product, or cosine distance,

    .. math:: fast_correlation
        \\frac{\\sum_{j=1}^m P(x_j, y_j) T(x_j, y_j)}{\\sqrt{\\sum_{j=1}^m T^2(x_j, y_j)}}

    .. math:: zero_mean_normalized_correlation
        \\frac{\\sum_{j=1}^m P(x_j, y_j) T(x_j, y_j)- avg(P)avg(T)}{\\sqrt{\\sum_{j=1}^m (T(x_j, y_j)-avg(T))^2+\sum_{j=1}^m P(x_j,y_j)-avg(P)}}
        for a template T and an experimental pattern P.

    Parameters
    ----------
    image : numpy.array
        The experimental diffraction pattern of interest.
    library : DiffractionLibrary
        The library of diffraction simulations to be correlated with the
        experimental data.
    n_largest : int
        The number of well correlated simulations to be retained.
    method : str
        Name of method used to compute correlation between templates and diffraction patterns. Can be
        'fast_correlation', 'full_frame_correlation' or 'zero_mean_normalized_correlation'. (ADDED in pyxem 0.11.0)
    mask : bool
        A mask for navigation axes. 1 indicates positions to be indexed.


    Returns
    -------
    top_matches : numpy.array
        Array of shape (<num phases>*n_largest, 3) containing the top n
        correlated simulations for the experimental pattern of interest, where
        each entry is on the form [phase index, [z, x, z], correlation].

    See Also
    --------
    IndexationGenerator.correlate

    Notes
    -----
    Correlation results are defined as,
        phase_index : int
            Index of the phase, following the ordering of the library keys
        [z, x, z] : ndarray
            numpy array of three floats, specifying the orientation in the
            Bunge convention, in degrees.
        correlation : float
            A coefficient of correlation, only normalised to the template
            intensity. This is in contrast to the reference work.

    References
    ----------
    E. F. Rauch and L. Dupuy, “Rapid Diffraction Patterns identification through
       template matching,” vol. 50, no. 1, pp. 87–99, 2005.

    A. Nakhmani and  A. Tannenbaum, "A New Distance Measure Based on Generalized Image Normalized Cross-Correlation
    for Robust Video Tracking and Image Recognition"
    Pattern Recognit Lett. 2013 Feb 1; 34(3): 315–321; doi: 10.1016/j.patrec.2012.10.025

    Discussion on Normalized cross correlation (xcdskd):
    https://xcdskd.readthedocs.io/en/latest/cross_correlation/cross_correlation_coefficient.html

    """

    top_matches = np.empty((len(library), n_largest, 3), dtype="object")

    if method == "zero_mean_normalized_correlation":
        nb_pixels = image.shape[0] * image.shape[1]
        average_image_intensity = np.average(image)
        image_std = np.linalg.norm(image - average_image_intensity)

    if mask == 1:
        for phase_index, library_entry in enumerate(library.values()):
            orientations = library_entry["orientations"]
            pixel_coords = library_entry["pixel_coords"]
            intensities = library_entry["intensities"]
            # TODO: This is only applicable some of the time, probably use an if + special_local in the for
            pattern_norms = library_entry["pattern_norms"]

            zip_for_locals = zip(orientations, pixel_coords, intensities, pattern_norms)

            or_saved, corr_saved = np.empty((n_largest, 3)), np.zeros((n_largest, 1))

            for (or_local, px_local, int_local, pn_local) in zip_for_locals:
                # TODO: Factorise out the generation of corr_local to a method='mthd' section
                # Extract experimental intensities from the diffraction image
                image_intensities = image[
                    px_local[:, 1], px_local[:, 0]
                ]  # Counter intuitive indexing? Why is it not px_local[:, 0], px_local[:, 1]?

                if method == "zero_mean_normalized_correlation":
                    corr_local = zero_mean_normalized_correlation(
                        nb_pixels,
                        image_std,
                        average_image_intensity,
                        image_intensities,
                        int_local,
                    )

                elif method == "fast_correlation":
                    corr_local = fast_correlation(
                        image_intensities, int_local, pn_local
                    )

                if corr_local > np.min(corr_saved):
                    or_saved[np.argmin(corr_saved)] = or_local
                    corr_saved[np.argmin(corr_saved)] = corr_local

                combined_array = np.hstack((or_saved, corr_saved))
                combined_array = combined_array[
                    np.flip(combined_array[:, 3].argsort())
                ]  # see stackoverflow/2828059 for details
                top_matches[phase_index, :, 0] = phase_index
                top_matches[phase_index, :, 2] = combined_array[:, 3]  # correlation
                for i in np.arange(n_largest):
                    top_matches[phase_index, i, 1] = combined_array[
                        i, :3
                    ]  # orientation

    return top_matches.reshape(-1, 3)


def index_magnitudes(z, simulation, tolerance):
    """Assigns hkl indices to peaks in the diffraction profile.

    Parameters
    ----------
    simulation : DiffractionProfileSimulation
        Simulation of the diffraction profile.
    tolerance : float
        The n orientations with the highest correlation values are returned.

    Returns
    -------
    indexation : np.array()
        indexation results.

    """
    mags = z
    sim_mags = np.array(simulation.magnitudes)
    sim_hkls = np.array(simulation.hkls)
    indexation = np.zeros(len(mags), dtype=object)

    for i in np.arange(len(mags)):
        diff = np.absolute((sim_mags - mags.data[i]) / mags.data[i] * 100)

        hkls = sim_hkls[np.where(diff < tolerance)]
        diffs = diff[np.where(diff < tolerance)]

        indices = np.array((hkls, diffs))
        indexation[i] = np.array((mags.data[i], indices))

    return indexation


def _choose_peak_ids(peaks, n_peaks_to_index):
    """Choose `n_peaks_to_index` indices from `peaks`.

    This implementation sorts by angle and then picks every
    len(peaks)/n_peaks_to_index element to get an even distribution of angles.

    Parameters
    ----------
    peaks : array_like
        Array of peak positions.
    n_peaks_to_index : int
        Number of indices to return.

    Returns
    -------
    peak_ids : numpy.array
        Array of indices of the chosen peaks.
    """
    r, angles = _cart2polar(peaks[:, 0], peaks[:, 1])
    return angles.argsort()[
        np.linspace(0, angles.shape[0] - 1, n_peaks_to_index, dtype=np.int)
    ]


def get_nth_best_solution(
    single_match_result, mode, rank=0, key="match_rate", descending=True
):
    """Get the nth best solution by match_rate from a pool of solutions

    Parameters
    ----------
    single_match_result : VectorMatchingResults, TemplateMatchingResults
        Pool of solutions from the vector matching algorithm
    mode : str
        'vector' or 'template'
    rank : int
        The rank of the solution, i.e. rank=2 returns the third best solution
    key : str
        The key to sort the solutions by, default = match_rate
    descending : bool
        Rank the keys from large to small

    Returns
    -------
    VectorMatching:
        best_fit : `OrientationResult`
            Parameters for the best fitting orientation
            Library Number, rotation_matrix, match_rate, error_hkls, total_error
    TemplateMatching: np.array
            Parameters for the best fitting orientation
            Library Number , [z, x, z], Correlation Score
    """
    if mode == "vector":
        try:
            best_fit = sorted(
                single_match_result[0].tolist(), key=attrgetter(key), reverse=descending
            )[rank]
        except AttributeError:
            best_fit = sorted(
                single_match_result.tolist(), key=attrgetter(key), reverse=descending
            )[rank]
    if mode == "template":
        srt_idx = np.argsort(single_match_result[:, 2])[::-1][rank]
        best_fit = single_match_result[srt_idx]

    return best_fit


def match_vectors(
    peaks, library, mag_tol, angle_tol, index_error_tol, n_peaks_to_index, n_best
):
    # TODO: Sort peaks by intensity or SNR
    """Assigns hkl indices to pairs of diffraction vectors.

    Parameters
    ----------
    peaks : np.array()
        The experimentally measured diffraction vectors, associated with a
        particular probe position, to be indexed. In Cartesian coordinates.
    library : VectorLibrary
        Library of reciprocal space vectors to be matched to the vectors.
    mag_tol : float
        Max allowed magnitude difference when comparing vectors.
    angle_tol : float
        Max allowed angle difference in radians when comparing vector pairs.
    index_error_tol : float
        Max allowed error in peak indexation for classifying it as indexed,
        calculated as :math:`|hkl_calculated - round(hkl_calculated)|`.
    n_peaks_to_index : int
        The maximum number of peak to index.
    n_best : int
        The maximum number of good solutions to be retained for each phase.

    Returns
    -------
    indexation : np.array()
        A numpy array containing the indexation results, each result consisting of 5 entries:
            [phase index, rotation matrix, match rate, error hkls, total error]

    """
    if peaks.shape == (1,) and peaks.dtype == np.object:
        peaks = peaks[0]

    # Assign empty array to hold indexation results. The n_best best results
    # from each phase is returned.
    top_matches = np.empty(len(library) * n_best, dtype="object")
    res_rhkls = []

    # Iterate over phases in DiffractionVectorLibrary and perform indexation
    # on each phase, storing the best results in top_matches.
    for phase_index, (phase, structure) in enumerate(
        zip(library.values(), library.structures)
    ):
        solutions = []
        lattice_recip = structure.lattice.reciprocal()
        phase_indices = phase["indices"]
        phase_measurements = phase["measurements"]

        if peaks.shape[0] < 2:  # pragma: no cover
            continue

        # Choose up to n_peaks_to_index unindexed peaks to be paired in all
        # combinations.
        # TODO: Matching can be done iteratively where successfully indexed
        #       peaks are removed after each iteration. This can possibly
        #       handle overlapping patterns.
        # unindexed_peak_ids = range(min(peaks.shape[0], n_peaks_to_index))
        # TODO: Better choice of peaks (longest, highest SNR?)
        # TODO: Inline after choosing the best, and possibly require external sorting (if using sorted)?
        unindexed_peak_ids = _choose_peak_ids(peaks, n_peaks_to_index)

        # Find possible solutions for each pair of peaks.
        for vector_pair_index, peak_pair_indices in enumerate(
            list(combinations(unindexed_peak_ids, 2))
        ):
            # Consider a pair of experimental scattering vectors.
            q1, q2 = peaks[peak_pair_indices, :]
            q1_len, q2_len = np.linalg.norm(q1), np.linalg.norm(q2)

            # Ensure q1 is longer than q2 for consistent order.
            if q1_len < q2_len:
                q1, q2 = q2, q1
                q1_len, q2_len = q2_len, q1_len

            # Calculate the angle between experimental scattering vectors.
            angle = get_angle_cartesian(q1, q2)

            # Get library indices for hkls matching peaks within tolerances.
            # TODO: phase are object arrays. Test performance of direct float arrays
            tolerance_mask = np.abs(phase_measurements[:, 0] - q1_len) < mag_tol
            tolerance_mask[tolerance_mask] &= (
                np.abs(phase_measurements[tolerance_mask, 1] - q2_len) < mag_tol
            )
            tolerance_mask[tolerance_mask] &= (
                np.abs(phase_measurements[tolerance_mask, 2] - angle) < angle_tol
            )

            # Iterate over matched library vectors determining the error in the
            # associated indexation.
            if np.count_nonzero(tolerance_mask) == 0:
                continue

            # Reference vectors are cartesian coordinates of hkls
            reference_vectors = lattice_recip.cartesian(phase_indices[tolerance_mask])

            # Rotation from experimental to reference frame
            rotations = get_rotation_matrix_between_vectors(
                q1, q2, reference_vectors[:, 0], reference_vectors[:, 1]
            )

            # Index the peaks by rotating them to the reference coordinate
            # system. Use rotation directly since it is multiplied from the
            # right. Einsum gives list of peaks.dot(rotation).
            hklss = lattice_recip.fractional(np.einsum("ijk,lk->ilj", rotations, peaks))

            # Evaluate error of peak hkl indexation
            rhklss = np.rint(hklss)
            ehklss = np.abs(hklss - rhklss)
            valid_peak_mask = np.max(ehklss, axis=-1) < index_error_tol
            valid_peak_counts = np.count_nonzero(valid_peak_mask, axis=-1)
            error_means = ehklss.mean(axis=(1, 2))

            num_peaks = len(peaks)
            match_rates = (valid_peak_counts * (1 / num_peaks)) if num_peaks else 0

            possible_solution_mask = match_rates > 0
            solutions += [
                OrientationResult(
                    phase_index=phase_index,
                    rotation_matrix=R,
                    match_rate=match_rate,
                    error_hkls=ehkls,
                    total_error=error_mean,
                    scale=1.0,
                    center_x=0.0,
                    center_y=0.0,
                )
                for R, match_rate, ehkls, error_mean in zip(
                    rotations[possible_solution_mask],
                    match_rates[possible_solution_mask],
                    ehklss[possible_solution_mask],
                    error_means[possible_solution_mask],
                )
            ]

            res_rhkls += rhklss[possible_solution_mask].tolist()

        n_solutions = min(n_best, len(solutions))

        i = phase_index * n_best  # starting index in unfolded array

        if n_solutions > 0:
            top_n = sorted(solutions, key=attrgetter("match_rate"), reverse=True)[
                :n_solutions
            ]

            # Put the top n ranked solutions in the output array
            top_matches[i : i + n_solutions] = top_n

        if n_solutions < n_best:
            # Fill with dummy values
            top_matches[i + n_solutions : i + n_best] = [
                OrientationResult(
                    phase_index=0,
                    rotation_matrix=np.identity(3),
                    match_rate=0.0,
                    error_hkls=np.array([]),
                    total_error=1.0,
                    scale=1.0,
                    center_x=0.0,
                    center_y=0.0,
                )
                for x in range(n_best - n_solutions)
            ]

    # Because of a bug in numpy (https://github.com/numpy/numpy/issues/7453),
    # triggered by the way HyperSpy reads results (np.asarray(res), which fails
    # when the two tuple values have the same first dimension), we cannot
    # return a tuple directly, but instead have to format the result as an
    # array ourselves.
    res = np.empty(2, dtype=np.object)
    res[0] = top_matches
    res[1] = np.asarray(res_rhkls)
    return res


def crystal_from_template_matching(z_matches):
    """Takes template matching results for a single navigation position and
    returns the best matching phase and orientation with correlation and
    reliability to define a crystallographic map.

    Parameters
    ----------
    z_matches : numpy.array
        Template matching results in an array of shape (m,3) sorted by
        correlation (descending) within each phase, with entries
        [phase, [z, x, z], correlation]

    Returns
    -------
    results_array : numpy.array
        Crystallographic mapping results in an array of shape (3) with entries
        [phase, np.array((z, x, z)), dict(metrics)]

    """
    # Create empty array for results.
    results_array = np.empty(3, dtype="object")
    # Consider single phase and multi-phase matching cases separately
    if np.unique(z_matches[:, 0]).shape[0] == 1:
        # get best matching phase (there is only one here)
        results_array[0] = z_matches[0, 0]
        # get best matching orientation Euler angles
        results_array[1] = z_matches[0, 1]
        # get template matching metrics
        metrics = dict()
        metrics["correlation"] = z_matches[0, 2]
        metrics["orientation_reliability"] = (
            100 * (1 - z_matches[1, 2] / z_matches[0, 2])
            if z_matches[0, 2] > 0
            else 100
        )
        results_array[2] = metrics
    else:
        # get best matching result
        index_best_match = np.argmax(z_matches[:, 2])
        # get best matching phase
        results_array[0] = z_matches[index_best_match, 0]
        # get best matching orientation Euler angles.
        results_array[1] = z_matches[index_best_match, 1]
        # get second highest correlation orientation for orientation_reliability
        z = z_matches[z_matches[:, 0] == results_array[0]]
        second_orientation = np.partition(z[:, 2], -2)[-2]
        # get second highest correlation phase for phase_reliability
        z = z_matches[z_matches[:, 0] != results_array[0]]
        second_phase = np.max(z[:, 2])
        # get template matching metrics
        metrics = dict()
        metrics["correlation"] = z_matches[index_best_match, 2]
        metrics["orientation_reliability"] = 100 * (
            1 - second_orientation / z_matches[index_best_match, 2]
        )
        metrics["phase_reliability"] = 100 * (
            1 - second_phase / z_matches[index_best_match, 2]
        )
        results_array[2] = metrics

    return results_array


def crystal_from_vector_matching(z_matches):
    """Takes vector matching results for a single navigation position and
    returns the best matching phase and orientation with correlation and
    reliability to define a crystallographic map.

    Parameters
    ----------
    z_matches : numpy.array
        Template matching results in an array of shape (m,5) sorted by
        total_error (ascending) within each phase, with entries
        [phase, R, match_rate, ehkls, total_error]

    Returns
    -------
    results_array : numpy.array
        Crystallographic mapping results in an array of shape (3) with entries
        [phase, np.array((z, x, z)), dict(metrics)]
    """
    if z_matches.shape == (1,):  # pragma: no cover
        z_matches = z_matches[0]

    # Create empty array for results.
    results_array = np.empty(3, dtype="object")

    # get best matching phase
    best_match = get_nth_best_solution(
        z_matches, "vector", key="total_error", descending=False
    )
    results_array[0] = best_match.phase_index

    # get best matching orientation Euler angles
    results_array[1] = np.rad2deg(mat2euler(best_match.rotation_matrix, "rzxz"))

    # get vector matching metrics
    metrics = dict()
    metrics["match_rate"] = best_match.match_rate
    metrics["ehkls"] = best_match.error_hkls
    metrics["total_error"] = best_match.total_error

    # get second highest correlation phase for phase_reliability (if present)
    other_phase_matches = [
        match for match in z_matches if match.phase_index != best_match.phase_index
    ]

    if other_phase_matches:
        second_best_phase = sorted(
            other_phase_matches, key=attrgetter("total_error"), reverse=False
        )[0]

        metrics["phase_reliability"] = 100 * (
            1 - best_match.total_error / second_best_phase.total_error
        )

        # get second best matching orientation for orientation_reliability
        same_phase_matches = [
            match for match in z_matches if match.phase_index == best_match.phase_index
        ]
        second_match = sorted(
            same_phase_matches, key=attrgetter("total_error"), reverse=False
        )[1]
    else:
        # get second best matching orientation for orientation_reliability
        second_match = get_nth_best_solution(
            z_matches, "vector", rank=1, key="total_error", descending=False
        )

    metrics["orientation_reliability"] = 100 * (
        1 - best_match.total_error / (second_match.total_error or 1.0)
    )

    results_array[2] = metrics

    return results_array


def get_phase_name_and_index(library):
    """Get a dictionary of phase names and its corresponding index value in library.keys().

    Parameters
    ----------
    library : DiffractionLibrary
        Diffraction library containing the phases and rotations

    Returns
    -------
    phase_name_index_dict : Dictionary {str : int}
    typically on the form {'phase_name 1' : 0, 'phase_name 2': 1, ...}
    """

    phase_name_index_dict = dict([(y, x) for x, y in enumerate(list(library.keys()))])
    return phase_name_index_dict


def peaks_from_best_template(single_match_result, library, rank=0):
    """Takes a TemplateMatchingResults object and return the associated peaks,
    to be used in combination with map().

    Parameters
    ----------
    single_match_result : ndarray
        An entry in a TemplateMatchingResults.
    library : DiffractionLibrary
        Diffraction library containing the phases and rotations.
    rank : int
        Get peaks from nth best orientation (default: 0, best vector match)

    Returns
    -------
    peaks : array
        Coordinates of peaks in the matching results object in calibrated units.
    """
    best_fit = get_nth_best_solution(single_match_result, "template", rank=rank)

    phase_names = list(library.keys())
    phase_index = int(best_fit[0])
    phase = phase_names[phase_index]
    simulation = library.get_library_entry(phase=phase, angle=tuple(best_fit[1]))["Sim"]

    peaks = simulation.coordinates[:, :2]  # cut z
    return peaks


def peaks_from_best_vector_match(single_match_result, library, rank=0):
    """Takes a VectorMatchingResults object and return the associated peaks,
    to be used in combination with map().

    Parameters
    ----------
    single_match_result : ndarray
        An entry in a VectorMatchingResults
    library : DiffractionLibrary
        Diffraction library containing the phases and rotations
    rank : int
        Get peaks from nth best orientation (default: 0, best vector match)

    Returns
    -------
    peaks : ndarray
        Coordinates of peaks in the matching results object in calibrated units.
    """
    best_fit = get_nth_best_solution(single_match_result, "vector", rank=rank)
    phase_index = best_fit.phase_index

    rotation_orientation = mat2euler(best_fit.rotation_matrix)
    # Don't change the original
    structure = library.structures[phase_index]
    sim = library.diffraction_generator.calculate_ed_data(
        structure,
        reciprocal_radius=library.reciprocal_radius,
        rotation=rotation_orientation,
        with_direct_beam=False,
    )

    # Cut z
    return sim.coordinates[:, :2]


##########################################################################
from pyxem.utils.dask_tools import _get_dask_array
import os
from dask.diagnostics import ProgressBar
from numba import njit, objmode, prange, guvectorize
from skimage.filters import gaussian
from skimage.transform import warp_polar

from pyxem.utils.polar_transform_utils import (get_polar_pattern_shape,
                                               image_to_polar,
                                               get_template_polar_coordinates,
                                               _chunk_to_polar)


def _simulations_to_arrays(simulations, max_radius = None):
    """
    Convert simulation results to arrays of diffraction spots
    
    Parameters
    ----------
    simulations : list 
        list of diffsims.sims.diffraction_simulation.DiffractionSimulation
        objects
    max_radius : float 
        limit to g-vector length in pixel coordinates
    
    Returns
    -------
    positions : numpy.ndarray (N, 2, R)
        An array containing all (x,y) coordinates of reflections of N templates. R represents
        the maximum number of reflections; templates containing fewer
        reflections are padded with 0's at the end.
    intensities : numpy.ndarray (N, R)
        An array containing all intensities of reflections of N templates normalized to
        unit length for each template.
    """
    num_spots = [i.intensities.shape[0] for i in simulations]
    max_spots = max(num_spots)
    positions = np.zeros((len(simulations), 2, max_spots), dtype=np.float64)
    intensities = np.zeros((len(simulations), max_spots), dtype=np.float64)
    for i, j in enumerate(simulations):
        x = j.calibrated_coordinates[:,0]
        y = j.calibrated_coordinates[:,1]
        intensity = j.intensities
        if max_radius is not None:
            condition = (x**2+y**2<max_radius**2)
            x = x[condition]
            y = y[condition]
            intensity = intensity[condition]
        positions[i, 0, :x.shape[0]]=x
        positions[i, 1, :y.shape[0]]=y
        intensities[i, :intensity.shape[0]]=intensity
    return positions, intensities


def _cartesian_positions_to_polar(x, y, delta_r=1, delta_theta=1):
    """Convert x, y coordinates to r, theta coordinates with integer
    values so they can be immediately queried in polar images"""
    imag = (x) + 1j * (y)
    r = np.rint(np.abs(imag) / delta_r).astype(np.int64)
    angle = np.rad2deg(np.angle(imag))
    theta = np.rint(np.mod(angle, 360) / delta_theta).astype(np.int64)
    return r, theta


@njit
def _extract_pixel_intensities(image, x, y):
    experimental = np.zeros(x.shape, dtype=np.float64)
    for j in prange(x.shape[-1]):
        experimental[j] = image[y[j], x[j]]
    return experimental


@njit
def _match_polar_to_polar_template(polar_image, r_template, theta_template, intensities):
    """Correlate a single polar template to a single polar image by shifting
    the template along the azimuthal axis. Return an array representing the
    correlation at each in-plane angle."""
    correlation = np.zeros(polar_image.shape[0], dtype=np.float64)
    n = polar_image.shape[0]*polar_image.shape[1]
    for i in prange(polar_image.shape[0]):
        theta_compare = np.mod(theta_template + i, polar_image.shape[0])
        image_intensities = _extract_pixel_intensities(polar_image, r_template, theta_compare)
        correlation[i] = _pearson_correlation(image_intensities, intensities, n)
        #correlation[i] = np.sum(np.multiply(image_intensities, intensities))
    return correlation


@njit
def _pearson_correlation(image_intensities, template_intensities, n):
    """Pearson correlation coefficient between image and template"""
    template_sum = np.sum(template_intensities)
    numerator = np.sum(np.multiply(image_intensities, template_intensities))
    denominator = np.sqrt(1-template_sum**2/n)
    return numerator/denominator


@njit
def _norm_array(ar):
    return ar/np.sqrt(np.sum(ar**2))


@njit(nogil=True)
def _match_polar_to_polar_library(polar_image, r_templates, theta_templates, intensities_templates):
    """Correlates a polar pattern to all polar templates and returns the
    correlation index and best fitting in-plane angles for each template.
    Each is returned as a 1D array"""
    correlations = np.zeros(intensities_templates.shape[0])
    angles = np.zeros(intensities_templates.shape[0])
    d_theta = 360 / polar_image.shape[0]
    for i in prange(intensities_templates.shape[0]):
        intensities_template = intensities_templates[i]
        r_template = r_templates[i]
        theta_template = theta_templates[i]
        match = _match_polar_to_polar_template(polar_image, r_template, theta_template, intensities_template)
        correlations[i] = np.max(match)
        angles[i] = np.argmax(match) * d_theta
    return correlations, angles


@njit
def _get_correlation_at_angle(polar_image, r_templates, theta_templates, intensities, angle_shifts):
    """Get the correlation between a polar image and the polar templates at particular theta-shifts"""
    correlations = np.zeros(r_templates.shape[0], dtype=np.float64)
    n = polar_image.shape[0]*polar_image.shape[1]
    for i in range(r_templates.shape[0]):
        angle = angle_shifts[i]
        r = r_templates[i]
        theta = np.mod(theta_templates[i] + angle, polar_image.shape[0])
        r = r.astype(np.int64)
        theta = theta.astype(np.int64)
        intensity = intensities[i]
        image_intensities = _extract_pixel_intensities(polar_image, r, theta)
        correlations[i] = _pearson_correlation(image_intensities, intensity, n)
        #correlations[i] = np.sum(np.multiply(image_intensities, intensity))
    return correlations
    

@njit
def _norm_intensities(intensities):
    norms = np.sqrt(np.sum(intensities**2, axis=1))
    intensities = (intensities.T / norms).T
    return intensities


def _norm_integrated_templates(integrated_templates):
    # normalize each template vector
    norm_integrated_templates = np.sqrt(np.sum(integrated_templates**2, axis=1))
    integrated_templates = (integrated_templates.T / norm_integrated_templates).T
    return integrated_templates


def _get_integrated_polar_templates(r_max, r_templates, intensities_templates):
    """Get an azimuthally integrated representation of the templates.
    Returns an array of shape (N, r_max) where r_max is the radial width
    of a polar image and N is the number of templates."""
    integrated_templates = np.zeros((r_templates.shape[0], r_max),
                                    dtype=np.float64)
    for i in range(intensities_templates.shape[0]):
        intensity = intensities_templates[i]
        r_template = r_templates[i]
        for j in range(intensity.shape[0]):
            inten = intensity[j]
            r_p = r_template[j]
            integrated_templates[i, r_p] = integrated_templates[i, r_p] + inten
    integrated_templates = _norm_integrated_templates(integrated_templates)
    return integrated_templates


@njit
def _match_library_to_polar_fast(polar_image, integrated_templates):
    """Compare a polar image to azimuthally integrated templates and return
    a 1D array of correlations with length = number of templates"""
    polar_sum = np.sum(polar_image, axis=0)
    polar_sum = polar_sum - np.mean(polar_sum)
    polar_sum = polar_sum / np.linalg.norm(polar_sum)
    coors = np.zeros(integrated_templates.shape[0], dtype=np.float64)
    n = polar_sum.shape[0]
    for i in range(integrated_templates.shape[0]):
        intensity = integrated_templates[i]
        coors[i] = _pearson_correlation(polar_sum, intensity, n)
        # coors[i] = np.sum(np.multiply(intensity, polar_sum))
    return coors


def _prepare_image_and_templates(image, simulations, delta_r, delta_theta,
        intensity_transform_function, find_direct_beam, **kwargs):
    """Prepare an image and template library for comparison"""
    polar_image = image_to_polar(image, delta_r, delta_theta, find_maximum=find_direct_beam, **kwargs)
    max_radius = polar_image.shape[1]*delta_r
    positions, intensities = _simulations_to_arrays(simulations, max_radius = max_radius)
    if intensity_transform_function is not None:
        intensities = intensity_transform_function(intensities)
        polar_image = intensity_transform_function(polar_image)
    polar_image = polar_image - np.mean(polar_image)
    polar_image = _norm_array(polar_image)
    intensities = _norm_intensities(intensities)
    r, theta = _cartesian_positions_to_polar(positions[:,0], positions[:,1], delta_r, delta_theta)
    return polar_image, r, theta, intensities


@njit(["float64[:,:](float64[:,:], float64[:,:], int64[:,:], int64[:,:], float64[:,:], float64, int64)"])
def _mixed_matching_lib_to_polar(polar_image, integrated_templates, r_templates,
                                 theta_templates, intensities_templates, fraction,
                                 n_best):
    """Match a polar image to all polar templates but first filter based on
    the azimuthally integrated patterns. Take only the (1-fraction)*100% of
    patterns to do a full indexation on. Return the first n_best answers."""
    coors = _match_library_to_polar_fast(polar_image, integrated_templates)
    template_indexes = np.arange(theta_templates.shape[0])
    lowest = np.percentile(coors, fraction * 100)
    condition = coors > lowest
    r_templates_filter = r_templates[condition]
    theta_templates_filter = theta_templates[condition]
    intensities_templates_filter = intensities_templates[condition]
    template_indexes_filter = template_indexes[condition]
    full_cors, full_angles = _match_polar_to_polar_library(polar_image,
                                                           r_templates_filter,
                                                           theta_templates_filter,
                                                           intensities_templates_filter,
                                                           )
    answer = np.empty((n_best, 3), dtype=np.float64)
    if n_best == 1:
        max_index_filter = np.argmax(full_cors)
        max_cor = full_cors[max_index_filter]
        max_angle = full_angles[max_index_filter]
        max_index = template_indexes_filter[max_index_filter]
        answer[0] = np.array((max_index, max_cor, max_angle))
    else:
        # unfortunately numba does not support partition which could speed up
        # and avoid full sort
        indices_sorted = np.argsort(-full_cors)
        n_best_indices = indices_sorted[:n_best]
        for i in range(n_best):
            answer[i, 0] = template_indexes_filter[n_best_indices[i]]
            answer[i, 1] = full_cors[n_best_indices[i]]
            answer[i, 2] = full_angles[n_best_indices[i]]
    return answer


@njit(["float64[:,:,:,:](float64[:,:,:,:], float64[:,:], int64[:,:], int64[:,:], float64[:,:], float64, int64)"],
      nogil=True,
      parallel=True,
      )
def _index_chunk(polar_images, integrated_templates, r_templates, theta_templates, intensities_templates, fraction, n_best):
    indexation_result_chunk = np.empty((polar_images.shape[0], polar_images.shape[1], n_best, 3), dtype=np.float64)
    for idx in prange(polar_images.shape[0]):
        for idy in prange(polar_images.shape[1]):
            pattern = polar_images[idx, idy]
            # compute indexation_result
            indexresult = _mixed_matching_lib_to_polar(pattern,
                                                       integrated_templates,
                                                       r_templates,
                                                       theta_templates,
                                                       intensities_templates,
                                                       fraction,
                                                       n_best
                                                       )
            indexation_result_chunk[idx, idy] = indexresult
    return indexation_result_chunk


@njit
def _renormalize_polar_block(polar_chunk):
    normed_polar = np.zeros_like(polar_chunk)
    for i in np.ndindex(polar_chunk.shape[:-2]):
        polar_image = polar_chunk[i]
        polar_image = polar_image - np.mean(polar_image)
        polar_image = _norm_array(polar_image)
        normed_polar[i] = polar_image
    return normed_polar


def get_in_plane_rotation_correlation(image, simulation,
                                   intensity_transform_function,
                                      delta_r=1, delta_theta=1, find_direct_beam=False, **kwargs):
    """
    Correlate a single image and simulation over the in-plane rotation angle

    Parameters
    ----------
    image : 2D numpy.ndarray
        The image of the diffraction pattern
    simulation : diffsims.sims.diffraction_simulation.DiffractionSimulation
        The diffraction pattern simulation
    delta_r : float
        The sampling of the radial coordinate
    delta_theta : float
        The sampling of the azimuthal coordinate
    find_direct_beam : bool
        Whether to optimize the direct beam, otherwise the center of the image
        is chosen
    **kwargs: passed to the direct beam finding algorithm

    Returns
    -------
    angle_array : 1D np.ndarray
        The in-plane angles at which the correlation is calculated
    correlation_array : 1D np.ndarray
        The correlation corresponding to these angles
    """
    polar_image = image_to_polar(image, delta_r, delta_theta, find_maximum=find_direct_beam, **kwargs)
    r, theta = get_template_polar_coordinates(simulation, in_plane_angle=0.,
                                              delta_r=delta_r, delta_theta=delta_theta)
    r = np.rint(r).astype(np.int64)
    theta = np.rint(theta).astype(np.int64)
    condition = (r>0) & (r<polar_image.shape[1])
    intensity = simulation.intensities
    r = r[condition]
    theta = theta[condition]
    intensity = intensity[condition]
    if intensity_transform_function is not None:
        intensity = intensity_transform_function(intensity)
        intensity = _norm_array(intensity)
        polar_image = intensity_transform_function(polar_image)
    polar_image = polar_image - np.mean(polar_image)
    polar_image = _norm_array(polar_image)
    correlation_array = _match_polar_to_polar_template(polar_image, r, theta, intensity)
    angle_array = np.arange(correlation_array.shape[0])*delta_theta
    return angle_array, correlation_array


def correlate_library_to_pattern(image, simulations, delta_r=1, delta_theta=1,
                                    intensity_transform_function = None,
                                find_direct_beam=True, **kwargs):
    """
    Get the best angle and associated correlation values, as well as the correlation with the inverted templates
    
    Parameters
    ----------
    image : 2d numpy.ndarray
        The pattern
    simulations : list of diffsims.sims.diffraction_simulation.DiffractionSimulation
        The diffraction pattern simulation
    delta_r : float
        The sampling of the radial coordinate
    delta_theta : float
        The sampling of the azimuthal coordinate
    find_direct_beam : bool
        Whether to optimize the direct beam, otherwise the center of the image
        is chosen
    **kwargs: passed to the direct beam finding algorithm

    Returns
    -------
    angles : 1D numpy.ndarray
        best fit in-plane angle for each template
    correlations : 1D numpy.ndarray
        best correlation for each template
    correlations_inverse : 1D numpy.ndarray
        correlation for inverse template of best fit
    """
    polar_image, r, theta, intensities = _prepare_image_and_templates(image, simulations, delta_r, delta_theta,
            intensity_transform_function, find_direct_beam, **kwargs)
    correlations, angles = _match_polar_to_polar_library(polar_image, r, theta, intensities)
    angles_180 = np.mod((angles+180)/delta_theta, 360/delta_theta).astype(np.uint64)
    correlations_inverse = _get_correlation_at_angle(polar_image, r, theta, intensities, angles_180)
    return angles, correlations, correlations_inverse


def correlate_library_to_pattern_fast(image, simulations, delta_r=1, delta_theta=1,
                                        intensity_transform_function = None,
                                      find_direct_beam=False, **kwargs):
    """
    Get the correlation between azimuthally integrated templates and patterns
    
    Parameters
    ----------
    image : 2d numpy.ndarray
        The pattern
    simulations : list of diffsims.sims.diffraction_simulation.DiffractionSimulation
        The diffraction pattern simulation
    delta_r : float
        The sampling of the radial coordinate
    delta_theta : float
        The sampling of the azimuthal coordinate
    find_direct_beam : bool
        Whether to optimize the direct beam, otherwise the center of the image
        is chosen
    **kwargs: passed to the direct beam finding algorithm

    Returns
    -------
    correlations : 1D numpy.ndarray
        azimuthally integrated correlation for each template
    """
    polar_image, r, theta, intensities = _prepare_image_and_templates(image, simulations, delta_r, delta_theta,
            intensity_transform_function, find_direct_beam, **kwargs)
    integrated_templates = _get_integrated_polar_templates(polar_image.shape[1], r, intensities)
    correlations = _match_library_to_polar_fast(polar_image, integrated_templates)
    return correlations


def correlate_library_to_pattern_partial(image, simulations, keep=100,
                                        intensity_transform_function = None,
                                         delta_r=1, delta_theta=1, find_direct_beam=False, **kwargs):
    """
    Get the best angle and associated correlation values, as well as the correlation with the inverted templates
    
    Parameters
    ----------
    image : 2d numpy.ndarray
        The pattern
    simulations : list of diffsims.sims.diffraction_simulation.DiffractionSimulation
        The diffraction pattern simulation
    keep : float or int
        The number or fraction of templates to perform full indexation on
    delta_r : float
        The sampling of the radial coordinate
    delta_theta : float
        The sampling of the azimuthal coordinate
    find_direct_beam : bool
        Whether to optimize the direct beam, otherwise the center of the image
        is chosen
    **kwargs: passed to the direct beam finding algorithm

    Returns
    -------
    indexes : 1D numpy.ndarray
        indexes of templates on which a full calculation has been performed
    angles : 1D numpy.ndarray
        best fit in-plane angle for the top "keep" templates
    correlations : 1D numpy.ndarray
        best correlation for the top "keep" templates
    correlations_inverse : 1D numpy.ndarray
        correlation for inverse template of best fit
    """
    polar_image, r, theta, intensities = _prepare_image_and_templates(image, simulations, delta_r, delta_theta,
            intensity_transform_function, find_direct_beam, **kwargs)
    if keep >= 1.:
        fraction = max((theta.shape[0] - keep) / theta.shape[0], 0.)
    elif 0. < keep <= 1.:
        fraction = 1. - keep
    else:
        raise ValueError("keep should be an integer >1 or a float [0-1]")
    integrated_templates = _get_integrated_polar_templates(polar_image.shape[1], r, intensities)
    correlations_fast = _match_library_to_polar_fast(polar_image, integrated_templates)
    template_indexes = np.arange(theta.shape[0])
    lowest = np.percentile(correlations_fast, fraction * 100)
    condition = correlations_fast > lowest
    r_templates_filter = r[condition]
    theta_templates_filter = theta[condition]
    intensities_templates_filter = intensities[condition]
    template_indexes_filter = template_indexes[condition]
    full_cors, full_angles = _match_polar_to_polar_library(polar_image,
                                                           r_templates_filter,
                                                           theta_templates_filter,
                                                           intensities_templates_filter,
                                                           )
    full_angles_180 =  np.mod((full_angles+180)/delta_theta, 360/delta_theta).astype(np.uint64)
    full_cors_180 = _get_correlation_at_angle(polar_image, r_templates_filter,
                                              theta_templates_filter, intensities_templates_filter,
                                              full_angles_180)
    return template_indexes_filter, full_angles, full_cors, full_cors_180


def get_n_best_matches(image, simulations, n_best=1, keep=100, delta_r=1, delta_theta=1,
                       intensity_transform_function=None,
                       find_direct_beam=False, **kwargs):
    """
    Get the best angle and associated correlation values, as well as the correlation with the inverted templates
    
    Parameters
    ----------
    image : 2d numpy.ndarray
        The pattern
    simulations : list of diffsims.sims.diffraction_simulation.DiffractionSimulation
        The diffraction pattern simulation
    n_best : int
        Number of best solutions to return, in order of descending match
    keep : float or int
        The number or fraction of templates to perform full indexation on
    delta_r : float
        The sampling of the radial coordinate
    delta_theta : float
        The sampling of the azimuthal coordinate
    intensity_transform_function : Callable
        A function to apply both to the image pixels and template intensities,
        for example a logarithmic or square root function, before they are
        compared to each other.
    find_direct_beam : bool
        Whether to optimize the direct beam, otherwise the center of the image
        is chosen
    **kwargs: passed to the direct beam finding algorithm

    Returns
    -------
    indexes : 1D numpy.ndarray
        indexes of best fit templates
    angles : 1D numpy.ndarray
        corresponding best fit in-plane angles
    correlations : 1D numpy.ndarray
        corresponding correlation values
    """
    polar_image, r_templates, theta_templates, intensities = _prepare_image_and_templates(image, simulations, delta_r, delta_theta,
            intensity_transform_function, find_direct_beam, **kwargs)
    if keep >= 1.:
        fraction = max((theta_templates.shape[0] - keep) / theta_templates.shape[0], 0.)
    elif 0. < keep <= 1.:
        fraction = 1. - keep
    else:
        raise ValueError("keep should be an integer >1 or a float [0-1]")
    integrated_templates = _get_integrated_polar_templates(polar_image.shape[1], r_templates, intensities)
    answer = _mixed_matching_lib_to_polar(polar_image, integrated_templates, r_templates,
                                 theta_templates, intensities, fraction, n_best)
    indices = answer[:, 0].astype(np.int64)
    cors = answer[:, 1]
    angles = answer[:, 2]
    return indices, cors, angles


def index_dataset_with_template_rotation(signal,
                                         library,
                                         phases,
                                         optimize_direct_beam=True,
                                         chunks="auto",
                                         delta_r=1,
                                         delta_theta=1,
                                         n_best=1,
                                         keep=100,
                                         intensity_transform_function=None,
                                         parallel_workers="auto",
                                         **kwargs):
    if parallel_workers == "auto":
        workers = os.cpu_count()
    else:
        workers = parallel_workers
    if not isinstance(workers, int):
        raise TypeError("Number of workers should be an integer")
    result = {}
    # get the dataset as a dask array and rechunk if necessary
    data = _get_dask_array(signal)
    # check if we have a 4D dataset, and if not, make it
    navdim = signal.axes_manager.navigation_dimension
    if navdim==0:
        data = data[np.newaxis, np.newaxis, ...]
    elif navdim==1:
        data = data[np.newaxis, ...]
    elif navdim==2:
        pass
    else:
        raise ValueError(f"Dataset has {navdim} navigation dimensions, max "
                         "is 2")
    if chunks is None:
        pass
    elif chunks == "auto":
        data = data.rechunk({0: "auto", 1: "auto", 2: None, 3: None})
    else:
        data = data.rechunk(chunks)
    # convert to polar dataset and normalize images
    r_dim, theta_dim = get_polar_pattern_shape(data.shape[-2:],
                                                delta_r,
                                                delta_theta)
    polar_chunking = (data.chunks[0], data.chunks[1], theta_dim, r_dim)
    polar_data = data.map_blocks(_chunk_to_polar,
                                 delta_r,
                                 delta_theta,
                                 optimize_direct_beam,
                                 dtype=np.float64,
                                 drop_axis=signal.axes_manager.signal_indices_in_array,
                                 chunks=polar_chunking,
                                 new_axis=(2, 3),
                                 )
    
    if intensity_transform_function is not None:
        polar_data = polar_data.map_blocks(intensity_transform_function,
                                           dtype=np.float64,)
        polar_data = polar_data.map_blocks(_renormalize_polar_block,
                                           dtype=np.float64,)
    max_radius = int(np.ceil(np.sqrt((data.shape[-1] / 2)**2 + (data.shape[-2] / 2)**2)))
    for phase_key in phases:
        phase_library = library[phase_key]
        positions, intensities = _simulations_to_arrays(phase_library["simulations"], max_radius)
        if intensity_transform_function is not None:
            intensities = intensity_transform_function(intensities)
            intensities = _norm_intensities(intensities)
        x = positions[:,0]
        y = positions[:,1]
        r, theta = _cartesian_positions_to_polar(x, y,
                                                 delta_r=delta_r,
                                                 delta_theta=delta_theta)
        # integrated intensity library for fast comparison
        integrated_templates = _get_integrated_polar_templates(max_radius, r, intensities)
        if keep >= 1.:
            fraction = max((theta.shape[0] - keep) / theta.shape[0], 0.)
        elif 0. < keep <= 1.:
            fraction = 1. - keep
        else:
            raise ValueError("keep should be an integer >1 or a float [0-1]")
        # map the indexation to the block
        indexation = polar_data.map_blocks(_index_chunk,
                                           integrated_templates,
                                           r,
                                           theta,
                                           intensities,
                                           fraction,
                                           n_best,
                                           dtype=np.float64,
                                           drop_axis=signal.axes_manager.signal_indices_in_array,
                                           chunks=(polar_data.chunks[0], polar_data.chunks[1], n_best, 3),
                                           new_axis=(2,3),
                                           )
        # wrangle data to (template_index), (orientation), (correlation)
        with ProgressBar():
            res_index = indexation.compute(scheduler="threads", num_workers=workers, optimize_graph=True)
        result[phase_key] = {}
        result[phase_key]["template_index"] = res_index[:,:,:,0]
        oris = phase_library["orientations"]
        orimap = oris[res_index[:,:,:,0].astype(np.uint64)]
        orimap[:,:,:,0] = res_index[:,:,:,2]
        result[phase_key]["orientation"] = orimap
        result[phase_key]["correlation"] = res_index[:, :, :, 1]
    return result
