import sparse
import scipy.special
import numpy as np
from opt_einsum import contract_expression
import ase.units as units
from .helper import timeit

DELTA_THRESHOLD = 2
IS_DELTA_CORRECTION_ENABLED = False
EVTOTENJOVERMOL = units.mol / (10 * units.J)
SCATTERING_MATRIX_FILE = 'scattering_matrix'
C_V_FILE = 'c_v.npy'
OCCUPATIONS_FILE = 'occupations.npy'

KELVINTOJOULE = units.kB / units.J
KELVINTOTHZ = units.kB / units.J / (2 * np.pi * units._hbar) * 1e-12





def calculate_broadening(velocity, cellinv, k_size):
    # we want the last index of velocity (the coordinate index to dot from the right to rlattice vec
    delta_k = cellinv / k_size
    base_sigma = ((np.tensordot(velocity, delta_k, [-1, 1])) ** 2).sum(axis=-1)
    base_sigma = np.sqrt(base_sigma / 6.)
    return base_sigma


def gaussian_delta(params):
    # alpha is a factor that tells whats the ration between the width of the gaussian
    # and the width of allowed phase space
    delta_energy = params[0]
    # allowing processes with width sigma and creating a gaussian with width sigma/2
    # we include 95% (erf(2/sqrt(2)) of the probability of scattering. The erf makes the total area 1
    sigma = params[1]
    if IS_DELTA_CORRECTION_ENABLED:
        correction = scipy.special.erf(DELTA_THRESHOLD / np.sqrt(2))
    else:
        correction = 1
    gaussian = 1 / np.sqrt(np.pi * sigma ** 2) * np.exp(- delta_energy ** 2 / (sigma ** 2))
    return gaussian / correction


def triangular_delta(params):
    delta_energy = np.abs(params[0])
    deltaa = np.abs(params[1])
    out = np.zeros_like(delta_energy)
    out[delta_energy < deltaa] = 1. / deltaa * (1 - delta_energy[delta_energy < deltaa] / deltaa)
    return out


def lorentzian_delta(params):
    delta_energy = params[0]
    gamma = params[1]
    if IS_DELTA_CORRECTION_ENABLED:
        # TODO: replace these hardcoded values
        # numerical value of the integral of a lorentzian over +- DELTA_TRESHOLD * gamma
        corrections = {
            1: 0.704833,
            2: 0.844042,
            3: 0.894863,
            4: 0.920833,
            5: 0.936549,
            6: 0.947071,
            7: 0.954604,
            8: 0.960263,
            9: 0.964669,
            10: 0.968195}
        correction = corrections[DELTA_THRESHOLD]
    else:
        correction = 1
    lorentzian = 1 / np.pi * 1 / 2 * gamma / (delta_energy ** 2 + (gamma / 2) ** 2)
    return lorentzian / correction


def calculate_single_gamma(is_plus, index_k, mu, index_kp_full, frequencies, density, nptk, first_evect, second_evect, first_chi, second_chi, scaled_potential, sigma_in,
                           frequencies_threshold, omegas, kpp_mapping, sigma_small, broadening_function):
    second_sign = (int(is_plus) * 2 - 1)

    omegas_difference = np.abs(omegas[index_k, mu] + second_sign * omegas[index_kp_full, :, np.newaxis] -
                               omegas[kpp_mapping, np.newaxis, :])

    condition = (omegas_difference < DELTA_THRESHOLD * 2 * np.pi * sigma_small) & \
                (frequencies[index_kp_full, :, np.newaxis] > frequencies_threshold) & \
                (frequencies[kpp_mapping, np.newaxis, :] > frequencies_threshold)
    interactions = np.array(np.where(condition)).T

    # TODO: Benchmark something fast like
    # interactions = np.array(np.unravel_index (np.flatnonzero (condition), condition.shape)).T
    if interactions.size != 0:
        # Create sparse index
        index_kp_vec = interactions[:, 0]
        index_kpp_vec = kpp_mapping[index_kp_vec]
        mup_vec = interactions[:, 1]
        mupp_vec = interactions[:, 2]


        if is_plus:
            dirac_delta = density[index_kp_vec, mup_vec] - density[index_kpp_vec, mupp_vec]

        else:
            dirac_delta = .5 * (1 + density[index_kp_vec, mup_vec] + density[index_kpp_vec, mupp_vec])

        dirac_delta /= (omegas[index_kp_vec, mup_vec] * omegas[index_kpp_vec, mupp_vec])
        if np.array(sigma_small).size == 1:

            dirac_delta *= broadening_function(
                [omegas_difference[index_kp_vec, mup_vec, mupp_vec], 2 * np.pi * sigma_small])

        else:
            dirac_delta *= broadening_function(
                [omegas_difference[index_kp_vec, mup_vec, mupp_vec], 2 * np.pi * sigma_small[
                    index_kp_vec, mup_vec, mupp_vec]])

        shapes = []
        for tens in scaled_potential, first_evect, first_chi, second_evect, second_chi:
            shapes.append(tens.shape)
        expr = contract_expression('litj,kni,kl,kmj,kt->knm', *shapes)
        scaled_potential = expr(scaled_potential,
                                first_evect,
                                first_chi,
                                second_evect,
                                second_chi
                                )

        scaled_potential = scaled_potential[index_kp_vec, mup_vec, mupp_vec]
        pot_times_dirac = np.abs(scaled_potential) ** 2 * dirac_delta

        #TODO: move units conversion somewhere else
        gammatothz = 1e11 * units.mol * EVTOTENJOVERMOL ** 2
        pot_times_dirac = units._hbar * np.pi / 4. * pot_times_dirac / omegas[index_k, mu] / nptk * gammatothz

        return index_kp_vec, mup_vec, index_kpp_vec, mupp_vec, pot_times_dirac, dirac_delta




class AnharmonicController:
    def __init__(self, phonons):
        self.phonons = phonons
        folder_name = self.phonons.folder_name
        folder_name += '/' + str(self.phonons.temperature) + '/'
        if self.phonons.is_classic:
            folder_name += 'classic/'
        else:
            folder_name += 'quantum/'
        self.folder_name = folder_name

        self._occupations = None
        self._c_v = None




    @property
    def occupations(self):
        return self._occupations

    @occupations.getter
    def occupations(self):
        if self._occupations is None:
            try:
                self._occupations = np.load (self.folder_name + OCCUPATIONS_FILE)
            except FileNotFoundError as e:
                print(e)
        if self._occupations is None:
            frequencies = self.frequencies

            temp = self.temperature * KELVINTOTHZ
            density = np.zeros_like(frequencies)
            physical_modes = frequencies > self.frequency_threshold

            if self.is_classic is False:
                density[physical_modes] = 1. / (np.exp(frequencies[physical_modes] / temp) - 1.)
            else:
                density[physical_modes] = temp / frequencies[physical_modes]
            self.occupations = density
        return self._occupations

    @occupations.setter
    def occupations(self, new_occupations):
        folder = self.folder_name
        np.save (folder + OCCUPATIONS_FILE, new_occupations)
        self._occupations = new_occupations


    @property
    def c_v(self):
        return self._c_v

    @c_v.getter
    def c_v(self):
        if self._c_v is None:
            try:
                folder = self.folder_name
                self._c_v = np.load (folder + C_V_FILE)
            except FileNotFoundError as e:
                print(e)
        if self._c_v is None:
            frequencies = self.frequencies
            c_v = np.zeros_like (frequencies)
            physical_modes = frequencies > self.frequency_threshold
            temperature = self.temperature * KELVINTOTHZ

            if (self.is_classic):
                c_v[physical_modes] = KELVINTOJOULE
            else:
                f_be = self.occupations
                c_v[physical_modes] = KELVINTOJOULE * f_be[physical_modes] * (f_be[physical_modes] + 1) * self.frequencies[physical_modes] ** 2 / \
                                      (temperature ** 2)
            self.c_v = c_v
        return self._c_v

    @c_v.setter
    def c_v(self, new_c_v):
        folder = self.folder_name
        np.save (folder + C_V_FILE, new_c_v)
        self._c_v = new_c_v


    @timeit
    def calculate_gamma(self, is_gamma_tensor_enabled=False):
        folder = self.phonons.folder_name
        if self.phonons.sigma_in is not None:
            folder += 'sigma_in_' + str(self.phonons.sigma_in).replace('.', '_') + '/'
        n_phonons = self.phonons.n_phonons
        is_plus_label = ['_0', '_1']
        self.phonons._gamma = np.zeros(n_phonons)
        self.phonons._ps = np.zeros(n_phonons)
        if is_gamma_tensor_enabled:
            self.phonons._gamma_tensor = np.zeros((n_phonons, n_phonons))
        for is_plus in [1, 0]:
            read_nu = -1
            file = None
            progress_filename = folder + '/' + SCATTERING_MATRIX_FILE + is_plus_label[is_plus]
            try:
                file = open(progress_filename, 'r+')
            except FileNotFoundError as err:
                print(err)
            else:
                for line in file:
                    read_nu, read_nup, read_nupp, value, value_ps = np.fromstring(line, dtype=np.float, sep=' ')
                    read_nu = int(read_nu)
                    read_nup = int(read_nup)
                    read_nupp = int(read_nupp)
                    self.phonons._gamma[read_nu] += value
                    self.phonons._ps[read_nu] += value_ps
                    if is_gamma_tensor_enabled:
                        if is_plus:
                            self.phonons.gamma_tensor[read_nu, read_nup] -= value
                            self.phonons.gamma_tensor[read_nu, read_nupp] += value
                        else:
                            self.phonons.gamma_tensor[read_nu, read_nup] += value
                            self.phonons.gamma_tensor[read_nu, read_nupp] += value

            atoms = self.phonons.atoms
            frequencies = self.phonons.frequencies
            velocities = self.phonons.velocities
            density = self.phonons.occupations
            k_size = self.phonons.kpts
            eigenvectors = self.phonons.eigenvectors
            list_of_replicas = self.phonons.list_of_replicas
            third_order = self.phonons.finite_difference.third_order
            sigma_in = self.phonons.sigma_in
            broadening = self.phonons.broadening_shape
            frequencies_threshold = self.phonons.frequency_threshold
            omegas = 2 * np.pi * frequencies

            nptk = np.prod(k_size)
            n_particles = atoms.positions.shape[0]

            print('Lifetime calculation')

            # TODO: We should write this in a better way
            if list_of_replicas.shape == (3,):
                n_replicas = 1
            else:
                n_replicas = list_of_replicas.shape[0]

            cell_inv = np.linalg.inv(atoms.cell)

            is_amorphous = (k_size == (1, 1, 1)).all()

            if is_amorphous:
                chi = 1
            else:
                rlattvec = cell_inv * 2 * np.pi
                cell_inv = np.linalg.inv(atoms.cell)
                replicated_cell = self.phonons.finite_difference.replicated_atoms.cell
                replicated_cell_inv = np.linalg.inv(replicated_cell)
                chi = np.zeros((nptk, n_replicas), dtype=np.complex)
                dxij = self.phonons.apply_boundary_with_cell(replicated_cell, replicated_cell_inv, list_of_replicas)

                for index_k in range(np.prod(k_size)):
                    i_k = np.array(np.unravel_index(index_k, k_size, order='C'))
                    k_point = i_k / k_size
                    realq = np.matmul(rlattvec, k_point)
                    chi[index_k] = np.exp(1j * dxij.dot(realq))

            print('Projection started')
            n_modes = n_particles * 3
            nptk = np.prod(k_size)
            masses = atoms.get_masses()
            rescaled_eigenvectors = eigenvectors[:, :, :].reshape((nptk, n_particles, 3, n_modes), order='C') / np.sqrt(
                masses[np.newaxis, :, np.newaxis, np.newaxis])
            rescaled_eigenvectors = rescaled_eigenvectors.reshape((nptk, n_particles * 3, n_modes), order='C')
            rescaled_eigenvectors = rescaled_eigenvectors.swapaxes(1, 2).reshape(nptk * n_modes, n_modes, order='C')

            index_kp_vec = np.arange(np.prod(k_size))
            i_kp_vec = np.array(np.unravel_index(index_kp_vec, k_size, order='C'))


            # TODO: find a way to use initial_mu correctly, when restarting
            read_nu += 1
            if (read_nu < nptk * n_modes):
                initial_k, initial_mu = np.unravel_index(read_nu, (nptk, n_modes))

                for index_k in range(initial_k, nptk):

                    i_k = np.array(np.unravel_index(index_k, k_size, order='C'))


                    i_kpp_vec = i_k[:, np.newaxis] + (int(is_plus) * 2 - 1) * i_kp_vec[:, :]
                    index_kpp_vec = np.ravel_multi_index(i_kpp_vec, k_size, order='C', mode='wrap')

                    if is_plus:
                        first_evect = rescaled_eigenvectors.reshape((nptk, n_modes, n_modes))
                    else:
                        first_evect = rescaled_eigenvectors.conj().reshape((nptk, n_modes, n_modes))
                    second_evect = rescaled_eigenvectors.conj().reshape((nptk, n_modes, n_modes))[index_kpp_vec]

                    if is_plus:
                        first_chi = chi
                    else:
                        first_chi = chi.conj()
                    second_chi = chi.conj()[index_kpp_vec]

                    if broadening == 'gauss':
                        broadening_function = gaussian_delta
                    elif broadening == 'lorentz':
                        broadening_function = lorentzian_delta
                    elif broadening == 'triangle':
                        broadening_function = triangular_delta

                    if sigma_in is None:
                        sigma_tensor_np = calculate_broadening(velocities[index_kp_vec, :, np.newaxis, :] -
                                                               velocities[index_kpp_vec, np.newaxis, :, :], cell_inv,
                                                               k_size)
                        sigma_small = sigma_tensor_np
                    else:
                        sigma_small = sigma_in


                    for mu in range(n_modes):
                        if index_k == initial_k and mu < initial_mu:
                            break

                        nu_single = np.ravel_multi_index([index_k, mu], [nptk, n_modes], order='C')
                        if not file:
                            file = open(progress_filename, 'a+')
                        if frequencies[index_k, mu] > frequencies_threshold:

                            scaled_potential = sparse.tensordot(third_order, rescaled_eigenvectors[nu_single, :], (0, 0))
                            scaled_potential = scaled_potential.reshape((n_replicas, n_modes, n_replicas, n_modes),
                                                                        order='C')

                            gamma_out = calculate_single_gamma(is_plus, index_k, mu, index_kp_vec, frequencies, density, nptk,
                                                               first_evect, second_evect, first_chi, second_chi,
                                                               scaled_potential, sigma_small, frequencies_threshold, omegas, index_kpp_vec, sigma_small, broadening_function)

                            if gamma_out:
                                index_kp_out, mup_out, index_kpp_out, mupp_out, pot_times_dirac, dirac = gamma_out
                                nup_vec = np.ravel_multi_index(np.array([index_kp_out, mup_out]),
                                                               np.array([nptk, n_modes]), order='C')
                                nupp_vec = np.ravel_multi_index(np.array([index_kpp_out, mupp_out]),
                                                                np.array([nptk, n_modes]), order='C')

                                self.phonons._gamma[nu_single] += pot_times_dirac.sum()
                                self.phonons._ps[nu_single] += dirac.sum()

                                for nup_index in range(nup_vec.shape[0]):
                                    nup = nup_vec[nup_index]
                                    nupp = nupp_vec[nup_index]
                                    if is_gamma_tensor_enabled:
                                        if is_plus:
                                            self.phonons._gamma_tensor[nu_single, nup] -= pot_times_dirac[nup_index]
                                            self.phonons._gamma_tensor[nu_single, nupp] += pot_times_dirac[nup_index]
                                        else:
                                            self.phonons._gamma_tensor[nu_single, nup] += pot_times_dirac[nup_index]
                                            self.phonons._gamma_tensor[nu_single, nupp] += pot_times_dirac[nup_index]

                                nu_vec = np.ones(nup_vec.shape[0]).astype(int) * nu_single
                                np.savetxt(file, np.vstack([nu_vec, nup_vec, nupp_vec, pot_times_dirac, dirac]).T, fmt='%i %i %i %.8e %.8e')
                file.close()

