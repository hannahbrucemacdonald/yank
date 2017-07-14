#!/usr/bin/env python

# =============================================================================
# MODULE DOCSTRING
# =============================================================================

"""
Tools to build Yank experiments from a YAML configuration file.

"""

# =============================================================================
# GLOBAL IMPORTS
# =============================================================================

import os
import copy
import yaml
import logging
import collections

import openmmtools as mmtools
import openmoltools as moltools
from simtk import unit, openmm
from simtk.openmm.app import PDBFile, AmberPrmtopFile
from schema import Schema, And, Or, Use, Optional, SchemaError

from . import utils, pipeline, mpi, restraints, repex
from .yank import AlchemicalPhase, Topography

logger = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

HIGHEST_VERSION = '1.3'  # highest version of YAML syntax


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def to_openmm_app(input_string):
    """Converter function to be used with validate_parameters()."""
    return getattr(openmm.app, input_string)


def convert_if_quantity(value):

    try:
        quantity = utils.quantity_from_string(value)
    except:
        return value
    return quantity


def update_nested_dict(original, updated):
    """Return a copy of a (possibly) nested dict of arbitrary depth"""
    new = original.copy()
    for key, value in updated.items():
        if isinstance(value, collections.Mapping):
            replacement = update_nested_dict(new.get(key, {}), value)
            new[key] = replacement
        else:
            new[key] = updated[key]
    return new


# ==============================================================================
# UTILITY CLASSES
# ==============================================================================

class YamlParseError(Exception):
    """Represent errors occurring during parsing of Yank YAML file."""
    def __init__(self, message):
        super(YamlParseError, self).__init__(message)
        logger.error(message)


class YankLoader(yaml.Loader):
    """PyYAML Loader that recognized !Combinatorial nodes and load OrderedDicts."""
    def __init__(self, *args, **kwargs):
        super(YankLoader, self).__init__(*args, **kwargs)
        self.add_constructor(u'!Combinatorial', self.combinatorial_constructor)
        self.add_constructor(u'!Ordered', self.ordered_constructor)

    @staticmethod
    def combinatorial_constructor(loader, node):
        """Constructor for YAML !Combinatorial entries."""
        return utils.CombinatorialLeaf(loader.construct_sequence(node))

    @staticmethod
    def ordered_constructor(loader, node):
        """Constructor for YAML !Ordered tag."""
        loader.flatten_mapping(node)
        return collections.OrderedDict(loader.construct_pairs(node))


class YankDumper(yaml.Dumper):
    """PyYAML Dumper that always return sequences in flow style and maps in block style."""
    def __init__(self, *args, **kwargs):
        super(YankDumper, self).__init__(*args, **kwargs)
        self.add_representer(utils.CombinatorialLeaf, self.combinatorial_representer)
        self.add_representer(collections.OrderedDict, self.ordered_representer)

    def represent_sequence(self, tag, sequence, flow_style=None):
        return yaml.Dumper.represent_sequence(self, tag, sequence, flow_style=True)

    def represent_mapping(self, tag, mapping, flow_style=None):
        return yaml.Dumper.represent_mapping(self, tag, mapping, flow_style=False)

    @staticmethod
    def combinatorial_representer(dumper, data):
        """YAML representer CombinatorialLeaf nodes."""
        return dumper.represent_sequence(u'!Combinatorial', data)

    @staticmethod
    def ordered_representer(dumper, data):
        """YAML representer OrderedDict nodes."""
        return dumper.represent_mapping(u'!Ordered', data)


# ==============================================================================
# BUILDER CLASS
# ==============================================================================

class AlchemicalPhaseFactory(object):

    DEFAULT_OPTIONS = {
        'anisotropic_dispersion_correction': True,
        'anisotropic_dispersion_cutoff': 'auto',
        'minimize': True,
        'minimize_tolerance': 1.0 * unit.kilojoules_per_mole/unit.nanometers,
        'minimize_max_iterations': 0,
        'randomize_ligand': False,
        'randomize_ligand_sigma_multiplier': 2.0,
        'randomize_ligand_close_cutoff': 1.5 * unit.angstrom,
        'number_of_equilibration_iterations': 0,
        'equilibration_timestep': 1.0 * unit.femtosecond,
        'checkpoint_interval': 10,
    }

    def __init__(self, sampler, thermodynamic_state, sampler_states, topography,
                 protocol, storage, restraint=None, alchemical_regions=None,
                 alchemical_factory=None, metadata=None, **options):
        self.sampler = sampler
        self.thermodynamic_state = thermodynamic_state
        self.sampler_states = sampler_states
        self.topography = topography
        self.protocol = protocol
        self.storage = storage
        self.restraint = restraint
        self.alchemical_regions = alchemical_regions
        self.alchemical_factory = alchemical_factory
        self.metadata = metadata
        self.options = self.DEFAULT_OPTIONS.copy()
        self.options.update(options)

    def create_alchemical_phase(self):
        alchemical_phase = AlchemicalPhase(self.sampler)
        create_kwargs = self.__dict__.copy()
        create_kwargs.pop('options')
        create_kwargs.pop('sampler')
        if not isinstance(self.storage, repex.Reporter):
            # Build the Reporter
            storage_path = create_kwargs.pop('storage')
            checkpoint_interval = self.options['checkpoint_interval']
            # We don't allow checkpoint file overwriting in YAML file
            reporter = repex.Reporter(storage_path, checkpoint_interval=checkpoint_interval)
            create_kwargs['storage'] = reporter
            self.storage = reporter
        if self.options['anisotropic_dispersion_correction'] is True:
            dispersion_cutoff = self.options['anisotropic_dispersion_cutoff']
        else:
            dispersion_cutoff = None
        alchemical_phase.create(anisotropic_dispersion_cutoff=dispersion_cutoff,
                                **create_kwargs)
        return alchemical_phase

    def initialize_alchemical_phase(self):
        alchemical_phase = self.create_alchemical_phase()

        # Minimize if requested.
        if self.options['minimize']:
            tolerance = self.options['minimize_tolerance']
            max_iterations = self.options['minimize_max_iterations']
            alchemical_phase.minimize(tolerance=tolerance, max_iterations=max_iterations)

        # Randomize ligand if requested.
        if self.options['randomize_ligand']:
            sigma_multiplier = self.options['randomize_ligand_sigma_multiplier']
            close_cutoff = self.options['randomize_ligand_close_cutoff']
            alchemical_phase.randomize_ligand(sigma_multiplier=sigma_multiplier,
                                              close_cutoff=close_cutoff)

        # Equilibrate if requested.
        if self.options['number_of_equilibration_iterations'] > 0:
            n_iterations = self.options['number_of_equilibration_iterations']
            mcmc_move = mmtools.mcmc.LangevinDynamicsMove(timestep=self.options['equilibration_timestep'],
                                                          collision_rate=90.0/unit.picosecond,
                                                          n_steps=500, reassign_velocities=True,
                                                          n_restart_attempts=6)
            alchemical_phase.equilibrate(n_iterations, mcmc_moves=mcmc_move)

        return alchemical_phase


class Experiment(object):
    """An experiment built by ExperimentBuilder."""
    def __init__(self, phases, number_of_iterations, switch_phase_interval):
        self.phases = phases
        self.number_of_iterations = number_of_iterations
        self.switch_phase_interval = switch_phase_interval
        self._phases_last_iterations = [None, None]

    @property
    def iteration(self):
        if None in self._phases_last_iterations:
            return 0
        return min(self._phases_last_iterations)

    def run(self, n_iterations=None):
        # Handle default argument.
        if n_iterations is None:
            n_iterations = self.number_of_iterations

        # Handle case in which we don't alternate between phases.
        if self.switch_phase_interval <= 0:
            switch_phase_interval = self.number_of_iterations

        # Count down the iterations to run.
        iterations_left = [None, None]
        while iterations_left != [0, 0]:

            # Alternate phases every switch_phase_interval iterations.
            for phase_id, phase in enumerate(self.phases):
                # Phases may get out of sync if the user delete the storage
                # file of only one phase and restart. Here we check that the
                # phase still has iterations to run before creating it.
                if self._phases_last_iterations[phase_id] == self.number_of_iterations:
                    iterations_left[phase_id] = 0
                    continue

                # If this is a new simulation, initialize alchemical phase.
                if isinstance(phase, AlchemicalPhaseFactory):
                    alchemical_phase = phase.initialize_alchemical_phase()
                    self.phases[phase_id] = phase.storage  # Should automatically be a Reporter class
                else:  # Resume previous simulation.
                    alchemical_phase = AlchemicalPhase.from_storage(phase)

                # Update total number of iterations. This may write the new number
                # of iterations in the storage file so we do it only if necessary.
                if alchemical_phase.number_of_iterations != self.number_of_iterations:
                    alchemical_phase.number_of_iterations = self.number_of_iterations

                # Determine number of iterations to run in this function call.
                if iterations_left[phase_id] is None:
                    total_iterations_left = self.number_of_iterations - alchemical_phase.iteration
                    iterations_left[phase_id] = min(n_iterations, total_iterations_left)

                # Run simulation for iterations_left or until we have to switch phase.
                iterations_to_run = min(iterations_left[phase_id], switch_phase_interval)
                alchemical_phase.run(n_iterations=iterations_to_run)

                # Update phase iteration info.
                iterations_left[phase_id] -= iterations_to_run
                self._phases_last_iterations[phase_id] = alchemical_phase.iteration

                # Do one last check to see if the phase has converged by other means (e.g. online analysis)
                if alchemical_phase.is_complete:
                    self._phases_last_iterations[phase_id] = 0

                # Delete alchemical phase and prepare switching.
                del alchemical_phase


class ExperimentBuilder(object):
    """Parse YAML configuration file and build the experiment.

    The relative paths indicated in the script are assumed to be relative to
    the script directory. However, if ExperimentBuilder is initiated with a string
    rather than a file path, the paths will be relative to the user's working
    directory.

    The class firstly perform a dry run to check if this is going to overwrite
    some files and raises an exception if it finds already existing output folders
    unless the options resume_setup or resume_simulation are True.

    Examples
    --------
    >>> import textwrap
    >>> import openmmtools as mmtools
    >>> import yank.utils
    >>> setup_dir = yank.utils.get_data_filename(os.path.join('..', 'examples',
    ...                                          'p-xylene-implicit', 'input'))
    >>> pxylene_path = os.path.join(setup_dir, 'p-xylene.mol2')
    >>> lysozyme_path = os.path.join(setup_dir, '181L-pdbfixer.pdb')
    >>> with mmtools.utils.temporary_directory() as tmp_dir:
    ...     yaml_content = '''
    ...     ---
    ...     options:
    ...       number_of_iterations: 1
    ...       output_dir: {}
    ...     molecules:
    ...       T4lysozyme:
    ...         filepath: {}
    ...       p-xylene:
    ...         filepath: {}
    ...         antechamber:
    ...           charge_method: bcc
    ...     solvents:
    ...       vacuum:
    ...         nonbonded_method: NoCutoff
    ...     systems:
    ...         my_system:
    ...             receptor: T4lysozyme
    ...             ligand: p-xylene
    ...             solvent: vacuum
    ...             leap:
    ...               parameters: [leaprc.gaff, leaprc.ff14SB]
    ...     protocols:
    ...       absolute-binding:
    ...         complex:
    ...           alchemical_path:
    ...             lambda_electrostatics: [1.0, 0.9, 0.8, 0.6, 0.4, 0.2, 0.0]
    ...             lambda_sterics: [1.0, 0.9, 0.8, 0.6, 0.4, 0.2, 0.0]
    ...         solvent:
    ...           alchemical_path:
    ...             lambda_electrostatics: [1.0, 0.8, 0.6, 0.3, 0.0]
    ...             lambda_sterics: [1.0, 0.8, 0.6, 0.3, 0.0]
    ...     experiments:
    ...       system: my_system
    ...       protocol: absolute-binding
    ...     '''.format(tmp_dir, lysozyme_path, pxylene_path)
    >>> yaml_builder = ExperimentBuilder(textwrap.dedent(yaml_content))
    >>> yaml_builder.run_experiments()

    """

    # --------------------------------------------------------------------------
    # Public API
    # --------------------------------------------------------------------------

    # These are options that can be specified only in the main "options" section.
    GENERAL_DEFAULT_OPTIONS = {
        'verbose': False,
        'resume_setup': False,
        'resume_simulation': False,
        'output_dir': 'output',
        'setup_dir': 'setup',
        'experiments_dir': 'experiments',
        'platform': 'fastest',
        'precision': 'auto',
        'switch_experiment_interval': 0,
    }

    # These options can be overwritten also in the "experiment"
    # section and they can be thus combinatorially expanded.
    EXPERIMENT_DEFAULT_OPTIONS = {
        'switch_phase_interval': 0,
        'temperature': 298 * unit.kelvin,
        'pressure': 1 * unit.atmosphere,
        'constraints': openmm.app.HBonds,
        'hydrogen_mass': 1 * unit.amu,
        'nsteps_per_iteration': 500,
        'timestep': 2.0 * unit.femtosecond,
        'collision_rate': 1.0 / unit.picosecond,
        'mc_displacement_sigma': 10.0 * unit.angstroms
    }

    def __init__(self, yaml_source=None):
        """Constructor.

        Parameters
        ----------
        yaml_source : str or dict
            A path to the YAML script or the YAML content. If not specified, you
            can load it later by using parse() (default is None).

        """
        self.options = self.GENERAL_DEFAULT_OPTIONS.copy()
        self.options.update(self.EXPERIMENT_DEFAULT_OPTIONS.copy())

        self._version = None
        self._script_dir = os.getcwd()  # basic dir for relative paths
        self._db = None  # Database containing molecules created in parse()
        self._raw_yaml = {}  # Unconverted input YAML script, helpful for
        self._expanded_raw_yaml = {}  # Raw YAML with selective keys chosen and blank dictionaries for missing keys
        self._protocols = {}  # Alchemical protocols description
        self._experiments = {}  # Experiments description

        # Parse YAML script
        if yaml_source is not None:
            self.parse(yaml_source)

    def update_yaml(self, yaml_source):
        """
        Update the current yaml content and reparse it

        Parameters
        ----------
        yaml_source

        """
        current_content = self._raw_yaml
        try:
            with open(yaml_source, 'r') as f:
                new_content = yaml.load(f, Loader=YankLoader)
        except IOError:  # string
            new_content = yaml.load(yaml_source, Loader=YankLoader)
        except TypeError:  # dict
            new_content = yaml_source.copy()
        combined_content = update_nested_dict(current_content, new_content)
        self.parse(combined_content)

    def parse(self, yaml_source):
        """Parse the given YAML configuration file.

        Validate the syntax and load the script into memory. This does not build
        the actual experiment.

        Parameters
        ----------
        yaml_source : str or dict
            A path to the YAML script or the YAML content.

        Raises
        ------
        YamlParseError
            If the input YAML script is syntactically incorrect.

        """
        # TODO check version of yank-yaml language
        # TODO what if there are multiple streams in the YAML file?
        # Load YAML script and decide working directory for relative paths
        try:
            with open(yaml_source, 'r') as f:
                yaml_content = yaml.load(f, Loader=YankLoader)
            self._script_dir = os.path.dirname(yaml_source)
        except IOError:  # string
            yaml_content = yaml.load(yaml_source, Loader=YankLoader)
        except TypeError:  # dict
            yaml_content = yaml_source.copy()

        self._raw_yaml = yaml_content.copy()

        # Check that YAML loading was successful
        if yaml_content is None:
            raise YamlParseError('The YAML file is empty!')
        if not isinstance(yaml_content, dict):
            raise YamlParseError('Cannot load YAML from source: {}'.format(yaml_source))

        # Check version (currently there's only one)
        try:
            self._version = yaml_content['version']
        except KeyError:
            self._version = HIGHEST_VERSION
        else:
            if self._version != HIGHEST_VERSION:
                raise ValueError('Unsupported syntax version {}'.format(self._version))

        # Expand combinatorial molecules and systems
        yaml_content = self._expand_molecules(yaml_content)
        yaml_content = self._expand_systems(yaml_content)

        # Save raw YAML content that will be needed when generating the YAML files
        self._expanded_raw_yaml = copy.deepcopy({key: yaml_content.get(key, {})
                                                 for key in ['options', 'molecules', 'solvents',
                                                             'systems', 'protocols']})

        # Validate options and overwrite defaults
        self.options.update(self._validate_options(yaml_content.get('options', {}),
                                                   validate_general_options=True))

        # Setup general logging
        utils.config_root_logger(self.options['verbose'], log_file_path=None)

        # Configure ContextCache, platform and precision. A Yank simulation
        # currently needs 3 contexts: 1 for the alchemical states and 2 for
        # the states with expanded cutoff.
        platform = self._configure_platform(self.options['platform'],
                                            self.options['precision'])
        try:
            mmtools.cache.global_context_cache.platform = platform
        except RuntimeError:
            # The cache has been already used. Empty it before switching platform.
            mmtools.cache.global_context_cache.empty()
            mmtools.cache.global_context_cache.platform = platform
        mmtools.cache.global_context_cache.capacity = 3

        # Initialize and configure database with molecules, solvents and systems
        setup_dir = os.path.join(self.options['output_dir'], self.options['setup_dir'])
        self._db = pipeline.SetupDatabase(setup_dir=setup_dir)
        self._db.molecules = self._validate_molecules(yaml_content.get('molecules', {}))
        self._db.solvents = self._validate_solvents(yaml_content.get('solvents', {}))
        self._db.systems = self._validate_systems(yaml_content.get('systems', {}))

        # Validate protocols
        self._protocols = self._validate_protocols(yaml_content.get('protocols', {}))

        # Validate experiments
        self._parse_experiments(yaml_content)

    def run_experiments(self):
        """Set up and run all the Yank experiments."""
        # Throw exception if there are no experiments
        if len(self._experiments) == 0:
            raise YamlParseError('No experiments specified!')

        # Handle case where we don't have to switch between experiments.
        if self.options['switch_experiment_interval'] <= 0:
            # Run Experiment for number_of_iterations.
            switch_experiment_interval = None
        else:
            switch_experiment_interval = self.options['switch_experiment_interval']

        # Setup and run all experiments with paths relative to the script directory
        with moltools.utils.temporary_cd(self._script_dir):
            self._check_resume()
            self._setup_experiments()

            # Cycle between experiments every switch_experiment_interval iterations
            # until all of them are done. We don't know how many experiments
            # there are until after the end of first for-loop.
            completed = [False]  # There always be at least one experiment.
            while not all(completed):
                for experiment_index, experiment in enumerate(self._build_experiments()):

                    experiment.run(n_iterations=switch_experiment_interval)

                    # Check if this experiment is done.
                    is_completed = experiment.iteration == experiment.number_of_iterations
                    try:
                        completed[experiment_index] = is_completed
                    except IndexError:
                        completed.append(is_completed)

    def build_experiments(self):
        """Set up, build and iterate over all the Yank experiments."""
        # Throw exception if there are no experiments
        if len(self._experiments) == 0:
            raise YamlParseError('No experiments specified!')

        # Setup and iterate over all experiments with paths relative to the script directory
        with moltools.utils.temporary_cd(self._script_dir):
            self._check_resume()
            self._setup_experiments()
            for experiment in self._build_experiments():
                yield experiment

    def setup_experiments(self):
        """Set up all Yank experiments without running them."""
        # Throw exception if there are no experiments
        if len(self._experiments) == 0:
            raise YamlParseError('No experiments specified!')

        # All paths must be relative to the script directory
        with moltools.utils.temporary_cd(self._script_dir):
            self._check_resume(check_experiments=False)
            self._setup_experiments()

    # --------------------------------------------------------------------------
    # Options handling
    # --------------------------------------------------------------------------

    def _determine_experiment_options(self, experiment):
        """Determine all the options required to build the experiment.

        Merge the options specified in the experiment section with the ones
        in the options section, and divide them into several dictionaries to
        feed to different main classes necessary to create an AlchemicalPhase.

        Parameters
        ----------
        experiment : dict
            The dictionary encoding the experiment.

        Returns
        -------
        experiment_options : dict
            The ExperimentBuilder experiment options. This does not contain
            the general ExperimentBuilder options that are accessible through
            self.options.
        phase_options : dict
            The options to pass to the AlchemicalPhaseFactory constructor.
        sampler_options : dict
            The options to pass to the ReplicaExchange constructor.
        alchemical_region_options : dict
            The options to pass to AlchemicalRegion.

        """
        # First discard general options.
        options = {name: value for name, value in self.options.items()
                   if name not in self.GENERAL_DEFAULT_OPTIONS}

        # Then update with specific experiment options.
        options.update(experiment.get('options', {}))

        def _filter_options(reference_options):
            return {name: value for name, value in options.items()
                    if name in reference_options}

        experiment_options = _filter_options(self.EXPERIMENT_DEFAULT_OPTIONS)
        phase_options = _filter_options(AlchemicalPhaseFactory.DEFAULT_OPTIONS)
        sampler_options = _filter_options(utils.get_keyword_args(repex.ReplicaExchange.__init__))
        alchemical_region_options = _filter_options(mmtools.alchemy._ALCHEMICAL_REGION_ARGS)

        return experiment_options, phase_options, sampler_options, alchemical_region_options

    # --------------------------------------------------------------------------
    # Combinatorial expansion
    # --------------------------------------------------------------------------

    def _expand_molecules(self, yaml_content):
        """Expand combinatorial molecules.

        Generate new YAML content with no combinatorial molecules. The new content
        is identical to the old one but combinatorial molecules are substituted by
        the description of all the non-combinatorial molecules that they generate.
        Moreover, systems that use combinatorial molecules are updated with the new
        molecules ids.

        Parameters
        ----------
        yaml_content : dict
            The YAML content as returned by yaml.load().

        Returns
        -------
        expanded_content : dict
            The new YAML content with combinatorial molecules expanded.

        """
        expanded_content = copy.deepcopy(yaml_content)

        if 'molecules' not in expanded_content:
            return expanded_content

        # First substitute all 'select: all' with the correct combination of indices
        for comb_mol_name, comb_molecule in utils.listitems(expanded_content['molecules']):
            if 'select' in comb_molecule and comb_molecule['select'] == 'all':
                # Get the number of models in the file
                extension = os.path.splitext(comb_molecule['filepath'])[1][1:]  # remove dot
                with moltools.utils.temporary_cd(self._script_dir):
                    if extension == 'pdb':
                        n_models = PDBFile(comb_molecule['filepath']).getNumFrames()

                    elif extension == 'csv' or extension == 'smiles':
                        n_models = len(pipeline.read_csv_lines(comb_molecule['filepath'], lines='all'))

                    elif extension == 'sdf' or extension == 'mol2':
                        if not utils.is_openeye_installed(oetools=('oechem',)):
                            err_msg = 'Molecule {}: Cannot "select" from {} file without OpenEye toolkit'
                            raise RuntimeError(err_msg.format(comb_mol_name, extension))
                        n_models = utils.read_oe_molecule(comb_molecule['filepath']).NumConfs()

                    else:
                        raise YamlParseError('Molecule {}: Cannot "select" from {} file'.format(
                            comb_mol_name, extension))

                # Substitute select: all with list of all models indices to trigger combinations
                comb_molecule['select'] = utils.CombinatorialLeaf(range(n_models))

        # Expand molecules and update molecule ids in systems
        expanded_content = utils.CombinatorialTree(expanded_content)
        update_nodes_paths = [('systems', '*', 'receptor'), ('systems', '*', 'ligand'),
                              ('systems', '*', 'solute')]
        expanded_content = expanded_content.expand_id_nodes('molecules', update_nodes_paths)

        return expanded_content

    def _expand_systems(self, yaml_content):
        """Expand combinatorial systems.

        Generate new YAML content with no combinatorial systems. The new content
        is identical to the old one but combinatorial systems are substituted by
        the description of all the non-combinatorial systems that they generate.
        Moreover, the experiments that use combinatorial systems are updated with
        the new system ids.

        Molecules must be already expanded when calling this function.

        Parameters
        ----------
        yaml_content : dict
            The YAML content as returned by _expand_molecules().

        Returns
        -------
        expanded_content : dict
            The new YAML content with combinatorial systems expanded.

        """
        expanded_content = copy.deepcopy(yaml_content)

        if 'systems' not in expanded_content:
            return expanded_content

        # Check if we have a sequence of experiments or a single one
        try:
            if isinstance(expanded_content['experiments'], list):  # sequence of experiments
                experiment_names = expanded_content['experiments']
            else:
                experiment_names = ['experiments']
        except KeyError:
            experiment_names = []

        # Expand molecules and update molecule ids in experiments
        expanded_content = utils.CombinatorialTree(expanded_content)
        update_nodes_paths = [(e, 'system') for e in experiment_names]
        expanded_content = expanded_content.expand_id_nodes('systems', update_nodes_paths)

        return expanded_content

    def _expand_experiments(self):
        """Generates all possible combinations of experiment.

        Each generated experiment is uniquely named.

        Returns
        -------
        output_dir : str
            A unique path where to save the experiment output files relative to
            the main output directory specified by the user in the options.
        combination : dict
            The dictionary describing a single experiment.

        """
        output_dir = ''
        for exp_name, experiment in utils.listitems(self._experiments):
            if len(self._experiments) > 1:
                output_dir = exp_name

            # Loop over all combinations
            for name, combination in experiment.named_combinations(separator='_', max_name_length=50):
                yield os.path.join(output_dir, name), combination

    # --------------------------------------------------------------------------
    # Parsing and syntax validation
    # --------------------------------------------------------------------------

    @classmethod
    def _validate_options(cls, options, validate_general_options):
        """Validate molecules syntax.

        Parameters
        ----------
        options : dict
            A dictionary with the options to validate.
        validate_general_options : bool
            If False only the options that can be specified in the
            experiment section are validated.

        Returns
        -------
        validated_options : dict
            The validated options.

        Raises
        ------
        YamlParseError
            If the syntax for any option is not valid.

        """
        template_options = cls.EXPERIMENT_DEFAULT_OPTIONS.copy()
        template_options.update(AlchemicalPhaseFactory.DEFAULT_OPTIONS)
        template_options.update(mmtools.alchemy._ALCHEMICAL_REGION_ARGS)
        template_options.update(utils.get_keyword_args(repex.ReplicaExchange.__init__))

        if validate_general_options is True:
            template_options.update(cls.GENERAL_DEFAULT_OPTIONS.copy())

        # Remove options that are not supported.
        template_options.pop('mcmc_moves')  # ReplicaExchange
        template_options.pop('alchemical_atoms')  # AlchemicalRegion
        template_options.pop('alchemical_bonds')
        template_options.pop('alchemical_angles')
        template_options.pop('alchemical_torsions')

        # Some options need to be treated differently.
        def check_anisotropic_cutoff(cutoff):
            if cutoff == 'auto':
                return cutoff
            else:
                return utils.process_unit_bearing_str(cutoff, unit.angstroms)
        special_conversions = {'constraints': to_openmm_app,
                               'anisotropic_dispersion_cutoff': check_anisotropic_cutoff}

        # Validate parameters
        try:
            validated_options = utils.validate_parameters(options, template_options, check_unknown=True,
                                                          process_units_str=True, float_to_int=True,
                                                          special_conversions=special_conversions)
        except (TypeError, ValueError) as e:
            raise YamlParseError(str(e))
        return validated_options

    @staticmethod
    def _validate_molecules(molecules_description):
        """Validate molecules syntax.

        Parameters
        ----------
        molecules_description : dict
            A dictionary representing molecules.

        Returns
        -------
        validated_molecules : dict
            The validated molecules description.

        Raises
        ------
        YamlParseError
            If the syntax for any molecule is not valid.

        """
        def is_peptide(filepath):
            """Input file is a peptide."""
            if not os.path.isfile(filepath):
                raise YamlParseError('File path does not exist.')
            extension = os.path.splitext(filepath)[1]
            if extension == '.pdb':
                return True
            return False

        def is_small_molecule(filepath):
            """Input file is a small molecule."""
            file_formats = frozenset(['mol2', 'sdf', 'smiles', 'csv'])
            if not os.path.isfile(filepath):
                raise YamlParseError('File path does not exist.')
            extension = os.path.splitext(filepath)[1][1:]
            if extension in file_formats:
                return True
            return False

        validated_molecules = molecules_description.copy()

        # Define molecules Schema
        epik_schema = utils.generate_signature_schema(moltools.schrodinger.run_epik,
                                                      update_keys={'select': int},
                                                      exclude_keys=['extract_range'])

        parameters_schema = {  # simple strings are converted to list of strings
            'parameters': And(Use(lambda p: [p] if isinstance(p, str) else p), [str])}
        common_schema = {Optional('leap'): parameters_schema, Optional('openeye'): {'quacpac': 'am1-bcc'},
                         Optional('antechamber'): {'charge_method': Or(str, None)},
                         Optional('epik'): epik_schema}
        molecule_schema = Or(
            utils.merge_dict({'smiles': str}, common_schema),
            utils.merge_dict({'name': str}, common_schema),
            utils.merge_dict({'filepath': is_small_molecule, Optional('select'): Or(int, 'all')},
                             common_schema),
            {'filepath': is_peptide, Optional('select'): Or(int, 'all'),
             Optional('leap'): parameters_schema, Optional('strip_protons'): bool}
        )

        # Schema validation
        for molecule_id, molecule_descr in utils.listitems(molecules_description):
            try:
                validated_molecules[molecule_id] = molecule_schema.validate(molecule_descr)

                # Check OpenEye charges - antechamber consistency
                if 'openeye' in validated_molecules[molecule_id]:
                    if not 'antechamber' in validated_molecules[molecule_id]:
                        raise YamlParseError('Cannot specify openeye charges without antechamber')
                    if validated_molecules[molecule_id]['antechamber']['charge_method'] is not None:
                        raise YamlParseError('Antechamber charge_method must be "null" to read '
                                             'OpenEye charges')

                # Convert epik "select" to "extract_range" which is accepted by run_epik()
                try:
                    extract_range = validated_molecules[molecule_id]['epik'].pop('select')
                    validated_molecules[molecule_id]['epik']['extract_range'] = extract_range
                except (AttributeError, KeyError):
                    pass

                # Create empty parameters list if not specified
                if 'leap' not in validated_molecules[molecule_id]:
                    validated_molecules[molecule_id]['leap'] = {'parameters': []}
            except SchemaError as e:
                raise YamlParseError('Molecule {}: {}'.format(molecule_id, e.autos[-1]))

        return validated_molecules

    @staticmethod
    def _validate_solvents(solvents_description):
        """Validate molecules syntax.

        Parameters
        ----------
        solvents_description : dict
            A dictionary representing solvents.

        Returns
        -------
        validated_solvents : dict
            The validated solvents description.

        Raises
        ------
        YamlParseError
            If the syntax for any solvent is not valid.

        """
        def to_explicit_solvent(nonbonded_method_str):
            """Check OpenMM explicit solvent."""
            openmm_app = to_openmm_app(nonbonded_method_str)
            if openmm_app == openmm.app.NoCutoff:
                raise ValueError('Nonbonded method cannot be NoCutoff.')
            return openmm_app

        def to_no_cutoff(nonbonded_method_str):
            """Check OpenMM implicit solvent or vacuum."""
            openmm_app = to_openmm_app(nonbonded_method_str)
            if openmm_app != openmm.app.NoCutoff:
                raise ValueError('Nonbonded method must be NoCutoff.')
            return openmm_app

        def is_supported_solvent_model(solvent_model):
            """Check that solvent model name is supported."""
            return solvent_model in pipeline._OPENMM_LEAP_SOLVENT_MODELS_MAP

        validated_solvents = solvents_description.copy()

        # Define solvents Schema
        explicit_schema = utils.generate_signature_schema(AmberPrmtopFile.createSystem,
                                update_keys={'nonbonded_method': Use(to_explicit_solvent)},
                                exclude_keys=['implicit_solvent'])
        explicit_schema.update({Optional('clearance'): Use(utils.to_unit_validator(unit.angstrom)),
                                Optional('solvent_model', default='tip4pew'): is_supported_solvent_model,
                                Optional('positive_ion'): str, Optional('negative_ion'): str})
        implicit_schema = utils.generate_signature_schema(AmberPrmtopFile.createSystem,
                                update_keys={'implicit_solvent': Use(to_openmm_app),
                                             Optional('nonbonded_method'): Use(to_no_cutoff)},
                                exclude_keys=['rigid_water'])
        vacuum_schema = utils.generate_signature_schema(AmberPrmtopFile.createSystem,
                                update_keys={'nonbonded_method': Use(to_no_cutoff)},
                                exclude_keys=['rigid_water', 'implicit_solvent'])
        solvent_schema = Schema(Or(explicit_schema, implicit_schema, vacuum_schema))

        # Schema validation
        for solvent_id, solvent_descr in utils.listitems(solvents_description):
            try:
                validated_solvents[solvent_id] = solvent_schema.validate(solvent_descr)
            except SchemaError as e:
                raise YamlParseError('Solvent {}: {}'.format(solvent_id, e.autos[-1]))

        return validated_solvents

    @staticmethod
    def _validate_protocols(protocols_description):
        """Validate protocols.

        Parameters
        ----------
        protocols_description : dict
            A dictionary representing protocols.

        Returns
        -------
        validated_protocols : dict
            The validated protocols description.

        Raises
        ------
        YamlParseError
            If the syntax for any protocol is not valid.

        """
        def sort_protocol(protocol):
            """Reorder phases in dictionary to have complex/solvent1 first."""
            sortables = [('complex', 'solvent'), ('solvent1', 'solvent2')]
            for sortable in sortables:
                # Phases names must be unambiguous, they can't contain both names
                phase1 = [(k, v) for k, v in utils.listitems(protocol)
                          if (sortable[0] in k and sortable[1] not in k)]
                phase2 = [(k, v) for k, v in utils.listitems(protocol)
                          if (sortable[1] in k and sortable[0] not in k)]

                # Phases names must be unique
                if len(phase1) == 1 and len(phase2) == 1:
                    return collections.OrderedDict([phase1[0], phase2[0]])

            # Could not find any sortable
            raise SchemaError('Phases must contain either "complex" and "solvent"'
                              'or "solvent1" and "solvent2"')

        validated_protocols = protocols_description.copy()

        # Define protocol Schema
        lambda_list = [And(float, lambda l: 0.0 <= l <= 1.0)]
        quantity_list = [Use(utils.quantity_from_string)]
        alchemical_path_schema = {'alchemical_path': {'lambda_sterics': lambda_list,
                                                      'lambda_electrostatics': lambda_list,
                                                      Optional(str): Or(lambda_list, quantity_list)}}
        protocol_schema = Schema(And(
            lambda v: len(v) == 2, {str: alchemical_path_schema},
            Or(collections.OrderedDict, Use(sort_protocol))
        ))

        # Schema validation
        for protocol_id, protocol_descr in utils.listitems(protocols_description):
            try:
                validated_protocols[protocol_id] = protocol_schema.validate(protocol_descr)
            except SchemaError as e:
                raise YamlParseError('Protocol {}: {}'.format(protocol_id, e.autos[-1]))

        return validated_protocols

    def _validate_systems(self, systems_description):
        """Validate systems.

        Receptors, ligands, and solvents must be already loaded. If they are not
        found an exception is raised.

        Parameters
        ----------
        yaml_content : dict
            The dictionary representing the YAML script loaded by yaml.load()

        Returns
        -------
        validated_systems : dict
            The validated systems description.

        Raises
        ------
        YamlParseError
            If the syntax for any experiment is not valid.

        """
        def is_known_molecule(molecule_id):
            if molecule_id in self._db.molecules:
                return True
            raise YamlParseError('Molecule ' + molecule_id + ' is unknown.')

        def is_known_solvent(solvent_id):
            if solvent_id in self._db.solvents:
                return True
            raise YamlParseError('Solvent ' + solvent_id + ' is unknown.')

        def is_pipeline_solvent(solvent_id):
            is_known_solvent(solvent_id)
            solvent = self._db.solvents[solvent_id]
            if (solvent['nonbonded_method'] != openmm.app.NoCutoff and
                    'clearance' not in solvent):
                raise YamlParseError('Explicit solvent {} does not specify '
                                     'clearance.'.format(solvent_id))
            return True

        def system_files(type):
            def _system_files(files):
                """Paths to amber/gromacs/xml files. Return them in alphabetical
                order of extension [*.inpcrd/gro/pdb, *.prmtop/top/xml]."""
                provided_extensions = [os.path.splitext(filepath)[1][1:] for filepath in files]
                if type == 'amber':
                    expected_extensions = ['inpcrd', 'prmtop']
                elif type == 'gromacs':
                    expected_extensions = ['gro', 'top']
                elif type == 'openmm':
                    expected_extensions = ['pdb', 'xml']

                # Check if extensions are expected.
                correct_type = sorted(provided_extensions) == sorted(expected_extensions)
                if not correct_type:
                    err_msg = ('Wrong system file types provided.\n'
                               'Extensions provided: {}\n'
                               'Expected extensions: {}').format(
                        sorted(provided_extensions), sorted(expected_extensions))
                    logger.debug(err_msg)
                    raise YamlParseError(err_msg)
                else:
                    logger.debug('Correctly recognized files {} as {}'.format(files, expected_extensions))

                # Check if given files exist.
                for filepath in files:
                    if not os.path.isfile(filepath):
                        logger.error('os.path.isfile({}) is False'.format(filepath))
                        raise YamlParseError('File path {} does not exist.'.format(filepath))

                # Return files in alphabetical order of extension.
                return [filepath for (ext, filepath) in sorted(zip(provided_extensions, files))]
            return _system_files

        # Define experiment Schema
        validated_systems = systems_description.copy()

        # Schema for leap parameters. Simple strings are converted to list of strings.
        parameters_schema = {'parameters': And(Use(lambda p: [p] if isinstance(p, str) else p), [str])}

        # Schema for DSL specification with system files.
        dsl_schema = {Optional('ligand_dsl'): str, Optional('solvent_dsl'): str}

        # System schema.
        system_schema = Schema(Or(
            {'receptor': is_known_molecule, 'ligand': is_known_molecule,
             'solvent': is_pipeline_solvent, Optional('pack', default=False): bool,
             Optional('leap'): parameters_schema},

            {'solute': is_known_molecule, 'solvent1': is_pipeline_solvent,
             'solvent2': is_pipeline_solvent, Optional('leap'): parameters_schema},

            utils.merge_dict(dsl_schema, {'phase1_path': Use(system_files('amber')),
                                          'phase2_path': Use(system_files('amber')),
                                          'solvent': is_known_solvent}),

            utils.merge_dict(dsl_schema, {'phase1_path': Use(system_files('amber')),
                                          'phase2_path': Use(system_files('amber')),
                                          'solvent1': is_known_solvent,
                                          'solvent2': is_known_solvent}),

            utils.merge_dict(dsl_schema, {'phase1_path': Use(system_files('gromacs')),
                                          'phase2_path': Use(system_files('gromacs')),
                                          'solvent': is_known_solvent,
                                          Optional('gromacs_include_dir'): os.path.isdir}),

            utils.merge_dict(dsl_schema, {'phase1_path': Use(system_files('gromacs')),
                                          'phase2_path': Use(system_files('gromacs')),
                                          'solvent1': is_known_solvent,
                                          'solvent2': is_known_solvent,
                                          Optional('gromacs_include_dir'): os.path.isdir}),

            utils.merge_dict(dsl_schema, {'phase1_path': Use(system_files('openmm')),
                                          'phase2_path': Use(system_files('openmm'))})
        ))

        # Schema validation
        for system_id, system_descr in utils.listitems(systems_description):
            try:
                validated_systems[system_id] = system_schema.validate(system_descr)

                # Create empty parameters list if not specified
                if 'leap' not in validated_systems[system_id]:
                    validated_systems[system_id]['leap'] = {'parameters': []}
            except SchemaError as e:
                raise YamlParseError('System {}: {}'.format(system_id, e.autos[-1]))

        return validated_systems

    def _parse_experiments(self, yaml_content):
        """Validate experiments.

        Perform dry run and validate system, protocol and options of every combination.

        Systems and protocols must be already loaded. If they are not found, an exception
        is raised. Experiments options are validated as well.

        Parameters
        ----------
        yaml_content : dict
            The dictionary representing the YAML script loaded by yaml.load()

        Raises
        ------
        YamlParseError
            If the syntax for any experiment is not valid.

        """
        def is_known_system(system_id):
            if system_id in self._db.systems:
                return True
            raise YamlParseError('System ' + system_id + ' is unknown.')

        def is_known_protocol(protocol_id):
            if protocol_id in self._protocols:
                return True
            raise YamlParseError('Protocol ' + protocol_id + ' is unknown')

        def validate_experiment_options(options):
            return ExperimentBuilder._validate_options(options, validate_general_options=False)

        # Check if there is a sequence of experiments or a single one
        try:
            if isinstance(yaml_content['experiments'], list):
                self._experiments = {exp_name: utils.CombinatorialTree(yaml_content[exp_name])
                                     for exp_name in yaml_content['experiments']}
            else:
                self._experiments = {'experiments': utils.CombinatorialTree(yaml_content['experiments'])}
        except KeyError:
            self._experiments = {}
            return

        # Restraint schema contains type and optional parameters.
        restraint_schema = {'type': Or(str, None), Optional(str): object}

        # Define experiment Schema
        experiment_schema = Schema({'system': is_known_system, 'protocol': is_known_protocol,
                                    Optional('options'): Use(validate_experiment_options),
                                    Optional('restraint'): restraint_schema})

        # Schema validation
        for experiment_id, experiment_descr in self._expand_experiments():
            try:
                experiment_schema.validate(experiment_descr)
            except SchemaError as e:
                raise YamlParseError('Experiment {}: {}'.format(experiment_id, e.autos[-1]))

    # --------------------------------------------------------------------------
    # File paths utilities
    # --------------------------------------------------------------------------

    def _get_experiment_dir(self, experiment_subdir):
        """Return the path to the directory where the experiment output files
        should be stored.

        Parameters
        ----------
        experiment_subdir : str
            The relative path w.r.t. the main experiments directory (determined
            through the options) of the experiment-specific subfolder.

        """
        return os.path.join(self.options['output_dir'], self.options['experiments_dir'],
                            experiment_subdir)

    # --------------------------------------------------------------------------
    # Resuming
    # --------------------------------------------------------------------------

    def _check_resume_experiment(self, experiment_dir, protocol_id):
        """Check if Yank output files already exist.

        Parameters
        ----------
        experiment_dir : str
            The path to the directory that should contain the output files.
        protocol_id : str
            The ID of the protocol used in the experiment.

        Returns
        -------
        bool
            True if NetCDF output files already exist, False otherwise.

        """
        # Build phases .nc file paths
        phase_names = self._protocols[protocol_id].keys()
        phase_paths = [os.path.join(experiment_dir, name + '.nc') for name in phase_names]

        # Look for existing .nc files in the folder
        for phase_path in phase_paths:
            if not (os.path.isfile(phase_path) and os.path.getsize(phase_path) > 0):
                return False
        return True

    @mpi.on_single_node(0, sync_nodes=True)
    def _check_resume(self, check_setup=True, check_experiments=True):
        """Perform dry run to check if we are going to overwrite files.

        If we find folders that ExperimentBuilder should create we raise an exception
        unless resume_setup or resume_simulation are found, in which case we
        assume we need to use the existing files. We never overwrite files, the
        user is responsible to delete them or move them.

        It's important to check all possible combinations at the beginning to
        avoid interrupting the user simulation after few experiments.

        Parameters
        ----------
        check_setup : bool
            Check if we are going to overwrite setup files (default is True).
        check_experiments : bool
            Check if we are going to overwrite experiment files (default is True).

        Raises
        ------
        YamlParseError
            If files to write already exist and we resuming options are not set.

        """
        err_msg = ''

        for exp_sub_dir, combination in self._expand_experiments():

            if check_experiments:
                resume_sim = self.options['resume_simulation']
                experiment_dir = self._get_experiment_dir(exp_sub_dir)
                if not resume_sim and self._check_resume_experiment(experiment_dir,
                                                                    combination['protocol']):
                    err_msg = 'experiment files in directory {}'.format(experiment_dir)
                    solving_option = 'resume_simulation'

            if check_setup and err_msg == '':
                resume_setup = self.options['resume_setup']
                system_id = combination['system']

                # Check system and molecule setup dirs
                is_sys_setup, is_sys_processed = self._db.is_system_setup(system_id)
                if is_sys_processed and not resume_setup:
                    system_dir = os.path.dirname(
                        self._db.get_system_files_paths(system_id)[0].position_path)
                    err_msg = 'system setup directory {}'.format(system_dir)
                elif not is_sys_setup:  # then this must go through the pipeline
                    try:  # binding free energy system
                        receptor_id = self._db.systems[system_id]['receptor']
                        ligand_id = self._db.systems[system_id]['ligand']
                        molecule_ids = [receptor_id, ligand_id]
                    except KeyError:  # partition/solvation free energy system
                        molecule_ids = [self._db.systems[system_id]['solute']]
                    for molecule_id in molecule_ids:
                        is_processed = self._db.is_molecule_setup(molecule_id)[1]
                        if is_processed and not resume_setup:
                            err_msg = 'molecule {} file'.format(molecule_id)
                            break

                if err_msg != '':
                    solving_option = 'resume_setup'

            # Check for errors
            if err_msg != '':
                err_msg += (' already exists; cowardly refusing to proceed. Move/delete '
                            'directory or set {} options').format(solving_option)
                raise YamlParseError(err_msg)

    # --------------------------------------------------------------------------
    # OpenMM Platform configuration
    # --------------------------------------------------------------------------

    @staticmethod
    def _opencl_device_support_precision(precision_model):
        """
        Check if this device supports the given precision model for OpenCL platform.

        Some OpenCL devices do not support double precision. This offers a test
        function.

        Returns
        -------
        is_supported : bool
            True if this device supports double precision for OpenCL, False
            otherwise.

        """
        opencl_platform = openmm.Platform.getPlatformByName('OpenCL')

        # Platforms are singleton so we need to store
        # the old precision model before modifying it
        old_precision = opencl_platform.getPropertyDefaultValue('OpenCLPrecision')

        # Test support by creating a toy context
        opencl_platform.setPropertyDefaultValue('Precision', precision_model)
        system = openmm.System()
        system.addParticle(1.0 * unit.amu)  # system needs at least 1 particle
        integrator = openmm.VerletIntegrator(1.0 * unit.femtoseconds)
        try:
            context = openmm.Context(system, integrator, opencl_platform)
            is_supported = True
        except Exception:
            is_supported = False
        else:
            del context
        del integrator

        # Restore old precision
        opencl_platform.setPropertyDefaultValue('Precision', old_precision)

        return is_supported

    @classmethod
    def _configure_platform(cls, platform_name, platform_precision):
        """
        Configure the platform to be used for simulation for the given precision.

        Parameters
        ----------
        platform_name : str
            The name of the platform to be used for execution. If 'fastest',
            the fastest available platform is used.
        platform_precision : str or None
            The precision to be used. If 'auto' the default value is used,
            which is always mixed precision except for Reference that only
            supports double precision, and OpenCL when the device supports
            only single precision. If None, the precision mode won't be
            set, so OpenMM default value will be used which is always
            'single' for CUDA and OpenCL.

        Returns
        -------
        platform : simtk.openmm.Platform
           The configured platform.

        Raises
        ------
        RuntimeError
            If the given precision model selected is not compatible with the
            platform.

        """
        # Determine the platform to configure
        if platform_name == 'fastest':
            platform = mmtools.utils.get_fastest_platform()
            platform_name = platform.getName()
        else:
            platform = openmm.Platform.getPlatformByName(platform_name)

        # Use only a single CPU thread if we are using the CPU platform.
        # TODO: Since there is an environment variable that can control this,
        # TODO: we may want to avoid doing this.
        mpicomm = mpi.get_mpicomm()
        if platform_name == 'CPU' and mpicomm is not None:
            logger.debug("Setting 'CpuThreads' to 1 because MPI is active.")
            platform.setPropertyDefaultValue('CpuThreads', '1')

        # If user doesn't specify precision, determine default value
        if platform_precision == 'auto':
            if platform_name == 'CUDA':
                platform_precision = 'mixed'
            elif platform_name == 'OpenCL':
                if cls._opencl_device_support_precision('mixed'):
                    platform_precision = 'mixed'
                else:
                    logger.info("This device does not support double precision for OpenCL. "
                                "Setting OpenCL precision to 'single'")
                    platform_precision = 'single'
            elif platform_name == 'Reference' or platform_name == 'CPU':
                platform_precision = None  # leave OpenMM default precision

        # Set platform precision
        if platform_precision is not None:
            logger.info("Setting {} platform to use precision model "
                        "'{}'.".format(platform_name, platform_precision))
            if platform_name == 'CUDA':
                platform.setPropertyDefaultValue('Precision', platform_precision)
            elif platform_name == 'OpenCL':
                # Some OpenCL devices do not support double precision so we need to test it
                if cls._opencl_device_support_precision(platform_precision):
                    platform.setPropertyDefaultValue('Precision', platform_precision)
                else:
                    raise RuntimeError('This device does not support double precision for OpenCL.')
            elif platform_name == 'Reference':
                if platform_precision != 'double':
                    raise RuntimeError("Reference platform does not support precision model '{}';"
                                       "only 'double' is supported.".format(platform_precision))
            elif platform_name == 'CPU':
                if platform_precision != 'mixed':
                    raise RuntimeError("CPU platform does not support precision model '{}';"
                                       "only 'mixed' is supported.".format(platform_precision))
            else:  # This is an unkown platform
                raise RuntimeError("Found unknown platform '{}'.".format(platform_name))

        return platform

    # --------------------------------------------------------------------------
    # Experiment setup and execution
    # --------------------------------------------------------------------------

    def _build_experiments(self):
        """Set up and build all the Yank experiments.

        IMPORTANT: This does not check if we are about to overwrite files, neither
        it creates the setup files nor it cds into the script directory! Use
        build_experiments() for that.

        """
        for output_dir, combination in self._expand_experiments():
            yield self._build_experiment(combination, output_dir)

    @mpi.on_single_node(rank=0, sync_nodes=True)
    def _setup_experiments(self):
        """Set up all experiments without running them.

        IMPORTANT: This does not check if we are about to overwrite files, nor it
        cd into the script directory! Use setup_experiments() for that.

        """
        # TODO parallelize setup
        for _, experiment in self._expand_experiments():
            # Force system and molecules setup
            system_id = experiment['system']
            sys_descr = self._db.systems[system_id]  # system description
            try:
                try:  # binding free energy system
                    components = (sys_descr['receptor'], sys_descr['ligand'], sys_descr['solvent'])
                except KeyError:  # partition/solvation free energy system
                    components = (sys_descr['solute'], sys_descr['solvent1'], sys_descr['solvent2'])
                logger.info('Setting up the systems for {}, {} and {}'.format(*components))
                self._db.get_system(system_id)
            except KeyError:  # system files are given directly by the user
                pass

    def _generate_yaml(self, experiment, file_path):
        """Generate the minimum YAML file needed to reproduce the experiment.

        Parameters
        ----------
        experiment : dict
            The dictionary describing a single experiment.
        file_path : str
            The path to the file to save.

        """
        yaml_dir = os.path.dirname(file_path)
        sys_descr = self._db.systems[experiment['system']]  # system description

        # Molecules section data
        try:
            try:  # binding free energy
                molecule_ids = [sys_descr['receptor'], sys_descr['ligand']]
            except KeyError:  # partition/solvation free energy
                molecule_ids = [sys_descr['solute']]
            mol_section = {mol_id: self._expanded_raw_yaml['molecules'][mol_id]
                           for mol_id in molecule_ids}

            # Copy to avoid modifying _expanded_raw_yaml when updating paths
            mol_section = copy.deepcopy(mol_section)
        except KeyError:  # user provided directly system files
            mol_section = {}

        # Solvents section data
        try:  # binding free energy
            solvent_ids = [sys_descr['solvent']]
        except KeyError:  # partition/solvation free energy
            try:
                solvent_ids = [sys_descr['solvent1'], sys_descr['solvent2']]
            except KeyError:  # from xml/pdb system files
                assert 'phase1_path' in sys_descr
                solvent_ids = []
        sol_section = {sol_id: self._expanded_raw_yaml['solvents'][sol_id]
                       for sol_id in solvent_ids}

        # Systems section data
        system_id = experiment['system']
        sys_section = {system_id: copy.deepcopy(self._expanded_raw_yaml['systems'][system_id])}

        # Protocols section data
        protocol_id = experiment['protocol']
        prot_section = {protocol_id: self._expanded_raw_yaml['protocols'][protocol_id]}

        # We pop the options section in experiment and merge it to the general one
        exp_section = experiment.copy()
        opt_section = self._expanded_raw_yaml['options'].copy()
        opt_section.update(exp_section.pop('options', {}))

        # Convert relative paths to new script directory
        for molecule in utils.listvalues(mol_section):
            if 'filepath' in molecule and not os.path.isabs(molecule['filepath']):
                molecule['filepath'] = os.path.relpath(molecule['filepath'], yaml_dir)

        try:  # systems for which user has specified directly system files
            for phase in ['phase2_path', 'phase1_path']:
                for path in sys_section[system_id][phase]:
                    sys_section[system_id][path] = os.path.relpath(path, yaml_dir)
        except KeyError:  # system went through pipeline
            pass

        try:  # output directory
            output_dir = opt_section['output_dir']
        except KeyError:
            output_dir = self.GENERAL_DEFAULT_OPTIONS['output_dir']
        if not os.path.isabs(output_dir):
            opt_section['output_dir'] = os.path.relpath(output_dir, yaml_dir)

        # If we are converting a combinatorial experiment into a
        # single one we must set the correct experiment directory
        experiment_dir = os.path.relpath(yaml_dir, output_dir)
        if experiment_dir != self.GENERAL_DEFAULT_OPTIONS['experiments_dir']:
            opt_section['experiments_dir'] = experiment_dir

        # Create YAML with the sections in order
        dump_options = {'Dumper': YankDumper, 'line_break': '\n', 'indent': 4}
        yaml_content = yaml.dump({'version': self._version}, explicit_start=True, **dump_options)
        yaml_content += yaml.dump({'options': opt_section}, **dump_options)
        if mol_section:
            yaml_content += yaml.dump({'molecules': mol_section},  **dump_options)
        if sol_section:
            yaml_content += yaml.dump({'solvents': sol_section},  **dump_options)
        yaml_content += yaml.dump({'systems': sys_section},  **dump_options)
        yaml_content += yaml.dump({'protocols': prot_section},  **dump_options)
        yaml_content += yaml.dump({'experiments': exp_section},  **dump_options)

        # Export YAML into a file
        with open(file_path, 'w') as f:
            f.write(yaml_content)

    @staticmethod
    def _save_analysis_script(results_dir, phase_names):
        """Store the analysis information about phase signs for analyze."""
        analysis = [[phase_names[0], 1], [phase_names[1], -1]]
        analysis_script_path = os.path.join(results_dir, 'analysis.yaml')
        with open(analysis_script_path, 'w') as f:
            yaml.dump(analysis, f)

    @mpi.on_single_node(rank=0, sync_nodes=True)
    def _safe_makedirs(self, directory):
        """Create directory and avoid race conditions.

        This is executed only on node 0 to avoid race conditions. The
        processes are synchronized at the end so that the non-0 nodes
        won't raise an IO error when trying to write a file in a non-
        existing directory.

        """
        # TODO when dropping Python 2, remove this and use os.makedirs(, exist_ok=True)
        if not os.path.isdir(directory):
            os.makedirs(directory)

    def _build_experiment(self, experiment, experiment_dir):
        """Prepare and run a single experiment.

        Parameters
        ----------
        experiment : dict
            A dictionary describing a single experiment
        experiment_dir : str
            The directory where to store the output files relative to the main
            output directory as specified by the user in the YAML script

        Returns
        -------
        yaml_experiment : Experiment
            A Experiment object.

        """
        system_id = experiment['system']
        protocol_id = experiment['protocol']
        exp_name = 'experiments' if experiment_dir == '' else os.path.basename(experiment_dir)

        # Get and validate experiment sub-options and divide them by class.
        exp_opts = self._determine_experiment_options(experiment)
        exp_opts, phase_opts, sampler_opts, alchemical_region_opts = exp_opts

        # Determine output directory and create it if it doesn't exist.
        results_dir = self._get_experiment_dir(experiment_dir)
        self._safe_makedirs(results_dir)

        # Configure logger file for this experiment.
        utils.config_root_logger(self.options['verbose'],
                                 os.path.join(results_dir, exp_name + '.log'))

        # Export YAML file for reproducibility
        mpi.run_single_node(0, self._generate_yaml, experiment,
                            os.path.join(results_dir, exp_name + '.yaml'))

        # Get ligand resname for alchemical atom selection. If we can't
        # find it, this is a solvation free energy calculation.
        ligand_dsl = None
        try:
            # First try for systems that went through pipeline.
            ligand_molecule_id = self._db.systems[system_id]['ligand']
        except KeyError:
            # Try with system from system files.
            try:
                ligand_dsl = self._db.systems[system_id]['ligand_dsl']
            except KeyError:
                # This is a solvation free energy.
                pass
        else:
            # Make sure that molecule filepath points to the mol2 file
            self._db.is_molecule_setup(ligand_molecule_id)
            ligand_descr = self._db.molecules[ligand_molecule_id]
            ligand_resname = utils.Mol2File(ligand_descr['filepath']).resname
            ligand_dsl = 'resname ' + ligand_resname

        if ligand_dsl is None:
            logger.debug('Cannot find ligand specification. '
                         'Alchemically modifying the whole solute.')
        else:
            logger.debug('DSL string for the ligand: "{}"'.format(ligand_dsl))

        # Determine solvent DSL.
        try:
            solvent_dsl = self._db.systems[system_id]['solvent_dsl']
        except KeyError:
            solvent_dsl = 'auto'  # Topography uses common solvent resnames.
        logger.debug('DSL string for the solvent: "{}"'.format(solvent_dsl))

        # Determine complex and solvent phase solvents
        try:  # binding free energy calculations
            solvent_ids = [self._db.systems[system_id]['solvent'],
                           self._db.systems[system_id]['solvent']]
        except KeyError:  # partition/solvation free energy calculations
            try:
                solvent_ids = [self._db.systems[system_id]['solvent1'],
                               self._db.systems[system_id]['solvent2']]
            except KeyError:  # from xml/pdb system files
                assert 'phase1_path' in self._db.systems[system_id]
                solvent_ids = [None, None]

        # Determine restraint description (None if not specified).
        restraint_descr = experiment.get('restraint')

        # Get system files.
        system_files_paths = self._db.get_system(system_id)
        gromacs_include_dir = self._db.systems[system_id].get('gromacs_include_dir', None)

        # Prepare Yank arguments
        phases = [None, None]
        # self._protocols[protocol_id] is an OrderedDict so phases are in the
        # correct order (e.g. [complex, solvent] or [solvent1, solvent2])
        phase_names = list(self._protocols[protocol_id].keys())
        for i, phase_name in enumerate(phase_names):
            # Check if we need to resume a phase. If the phase has been
            # already created, Experiment will resume from the storage.
            phase_path = os.path.join(results_dir, phase_name + '.nc')
            if os.path.isfile(phase_path):
                phases[i] = phase_path
                continue

            # Create system, topology and sampler state from system files.
            solvent_id = solvent_ids[i]
            positions_file_path = system_files_paths[i].position_path
            parameters_file_path = system_files_paths[i].parameters_path
            if solvent_id is None:
                system_options = None
            else:
                system_options = utils.merge_dict(self._db.solvents[solvent_id], exp_opts)
            logger.info("Reading phase {}".format(phase_name))
            system, topology, sampler_state = pipeline.read_system_files(
                positions_file_path, parameters_file_path, system_options,
                gromacs_include_dir=gromacs_include_dir)

            # Identify system components. There is a ligand only in the complex phase.
            if i == 0:
                ligand_atoms = ligand_dsl
            else:
                ligand_atoms = None
            topography = Topography(topology, ligand_atoms=ligand_atoms,
                                    solvent_atoms=solvent_dsl)

            # Create reference thermodynamic state.
            if system.usesPeriodicBoundaryConditions():
                pressure = exp_opts['pressure']
            else:
                pressure = None
            thermodynamic_state = mmtools.states.ThermodynamicState(system, exp_opts['temperature'],
                                                                    pressure=pressure)

            # Start from AlchemicalPhase default alchemical region
            # and modified it according to the user options.
            phase_protocol = self._protocols[protocol_id][phase_name]['alchemical_path']
            alchemical_region = AlchemicalPhase._build_default_alchemical_region(system, topography,
                                                                                 phase_protocol)
            alchemical_region = alchemical_region._replace(**alchemical_region_opts)

            # Apply restraint only if this is the first phase. AlchemicalPhase
            # will take care of raising an error if the phase type does not support it.
            if (i == 0 and restraint_descr is not None and restraint_descr['type'] is not None):
                restraint_type = restraint_descr['type']
                restraint_parameters = {par: convert_if_quantity(value) for par, value in restraint_descr.items()
                                        if par != 'type'}
                restraint = restraints.create_restraint(restraint_type, **restraint_parameters)
            else:
                restraint = None

            # Create MCMC moves and sampler. Apply MC rotation displacement to ligand.
            if len(topography.ligand_atoms) > 0:
                move_list = [
                    mmtools.mcmc.MCDisplacementMove(displacement_sigma=exp_opts['mc_displacement_sigma'],
                                                    atom_subset=topography.ligand_atoms),
                    mmtools.mcmc.MCRotationMove(atom_subset=topography.ligand_atoms)
                ]
            else:
                move_list = []
            move_list.append(mmtools.mcmc.LangevinDynamicsMove(timestep=exp_opts['timestep'],
                                                               collision_rate=exp_opts['collision_rate'],
                                                               n_steps=exp_opts['nsteps_per_iteration'],
                                                               reassign_velocities=True,
                                                               n_restart_attempts=6))
            mcmc_move = mmtools.mcmc.SequenceMove(move_list=move_list)
            sampler = repex.ReplicaExchange(mcmc_moves=mcmc_move, **sampler_opts)

            # Create phases.
            phases[i] = AlchemicalPhaseFactory(sampler, thermodynamic_state, sampler_state,
                                               topography, phase_protocol, storage=phase_path,
                                               restraint=restraint, alchemical_regions=alchemical_region,
                                               **phase_opts)

        # Dump analysis script
        mpi.run_single_node(0, self._save_analysis_script, results_dir, phase_names)

        # Return new Experiment object.
        return Experiment(phases, sampler_opts['number_of_iterations'],
                          exp_opts['switch_phase_interval'])


if __name__ == "__main__":
    import doctest
    doctest.testmod()
