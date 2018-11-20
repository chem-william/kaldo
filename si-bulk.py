import numpy as np
import ase
import ase.io
import ballistico.geometry_helper as geometry_helper
import ballistico.atoms_helper as atoms_helper
from ballistico.finite_difference import FiniteDifference
from ase.calculators.lammpslib import LAMMPSlib

import ballistico.io_helper as io_helper
import ballistico.constants as constants
from ase.phonons import Phonons
from ballistico.ballistico_phonons import BallisticoPhonons
from ballistico.conductivity_controller import ConductivityController
import matplotlib.pyplot as plt
from ballistico.plotter import Plotter
from ballistico.shengbte_phonons_controller import ShengbtePhononsController
from ballistico.interpolation_controller import interpolator
np.set_printoptions(suppress=True)

if __name__ == "__main__":
    # We start from a atoms
    atoms = ase.io.read ('si-bulk.xyz')
    
    # and replicate it
    supercell = np.array ([3, 3, 3])
    n_replicas = np.prod(supercell)

    # we create our system
    temperature = 300

    # our Phonons object built on the system
    kpts = np.array ([5, 5, 5])
    is_classic = False

    # import the calculated second order
    # second_order = io_helper.import_second_dlpoly (atoms, supercell)
    # second_order = ase_helper.calculate_second(atoms, supercell)

    # import the calculated third order
    # third_order = io_helper.import_third_order_dlpoly(atoms, supercell)
    # third_order = ase_helper.calculate_third(atoms, supercell)

    calculator_inputs = ["pair_style tersoff", "pair_coeff * * forcefields/Si.tersoff Si"]

    finite_difference = FiniteDifference(atoms=atoms,
                                         supercell=supercell,
                                         calculator=LAMMPSlib,
                                         calculator_inputs=calculator_inputs,
                                         is_persistency_enabled=False)
    

    phonons = BallisticoPhonons (finite_difference=finite_difference,
                                 kpts=kpts,
                                 is_classic=is_classic,
                                 temperature=temperature,
                                 sigma_in=None,
                                 is_persistency_enabled=False)


    Plotter (phonons, folder='plot/ballistico/').plot_everything()
    ConductivityController (phonons).calculate_conductivity (is_classical=is_classic)
    # shen_phonons = Shengbte_phonons (atoms=atoms,
    #                               supercell=supercell,
    #                               kpts=kpts,
    #                               is_classic=is_classic,
    #                               temperature=temperature,
    #                               second_order=second_order,
    #                               third_order=third_order,
    #                               # sigma_in=.1,
    #                               is_persistency_enabled=False)
    # print (shen_phonons.run ())
    # PlotViewController(shen_phonons, folder='plot/sheng/').plot_everything()
    # ConductivityController (shen_phonons).calculate_conductivity (is_classical=is_classic)
    # print (shen_phonons.read_conductivity(converged=False))