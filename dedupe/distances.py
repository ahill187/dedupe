# -*- coding: future_fstrings -*-

import pkgutil
import numpy
import copyreg
import types
import logging
import dedupe.variables
import dedupe.variables.base as base
from dedupe.variables.base import MissingDataType
from dedupe.variables.interaction import InteractionType
from timebudget import timebudget

for _, module, _ in pkgutil.iter_modules(dedupe.variables.__path__,
                                         'dedupe.variables.'):
    __import__(module)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
FIELD_CLASSES = {k: v for k, v in base.allSubclasses(base.FieldType) if k}
timebudget.set_quiet()


class Distances(object):

    def __init__(self, fields):

        primary_fields, variables = typifyFields(fields)
        self.primary_fields = primary_fields
        self._derived_start = len(variables)

        variables += interactions(fields, primary_fields)
        variables += missing(variables)

        self._missing_field_indices = missing_field_indices(variables)
        self._interaction_indices = interaction_indices(variables)
        self._interaction_weights = interaction_weights(variables)

        self._variables = variables

    def __len__(self):
        return len(self._variables)


    # Changing this from a property to just a normal attribute causes
    # pickling problems, because we are removing static methods from
    # their class context. This could be fixed by defining comparators
    # outside of classes in fieldclasses
    @property
    def _field_comparators(self):
        start = 0
        stop = 0
        comparators = []
        for field in self.primary_fields:
            stop = start + len(field)
            comparators.append((field.field, field.comparator,
                                field.weight, start, stop))
            start = stop

        return comparators

    def predicates(self, index_predicates=True, canopies=True):
        """
        Returns:
            predicates: (set)[dudupe.predicates class]
        """
        predicates = set()
        for definition in self.primary_fields:
            # logger.info(f"dedupe.distances L70: Definition: {definition}")
            for predicate in definition.predicates:
                # logger.info(f"dedupe.distances L72: Predicates: {predicate}")
                if hasattr(predicate, 'index'):
                    if index_predicates:
                        if hasattr(predicate, 'canopy'):
                            if canopies:
                                predicates.add(predicate)
                        else:
                            if not canopies:
                                predicates.add(predicate)
                else:
                    predicates.add(predicate)
        logger.info(f"number of predicates: {len(predicates)}")
        return predicates

    # @timebudget
    def compute_distance_matrix(self, record_pairs):
        """
        Args:
            record_pairs: (list)[list] ordered list of all the record pairs (distinct and match)
                ::

                    [
                        [record_1, record_2],
                        [record_1, record_3]
                    ]

        Returns:
            distance_matrix: (np.Array) 2D matrix
                # rows = # pairs
                # columns = # fields
        """
        num_records = len(record_pairs)
        distance_matrix = numpy.empty((num_records, len(self)), 'f4')
        field_comparators = self._field_comparators
        # ids_of_interest = {}

        for i, (record_1, record_2) in enumerate(record_pairs):

            for field, compare, weight, start, stop in field_comparators:
                if record_1[field] is not None and record_2[field] is not None:
                    distance_matrix[i, start:stop] = compare(record_1[field],
                                                       record_2[field])*weight
                elif hasattr(compare, 'missing'):
                    distance_matrix[i, start:stop] = compare(record_1[field],
                                                       record_2[field])*weight
                else:
                    distance_matrix[i, start:stop] = numpy.nan
            # if record_1['id'] in ['91', '92', '93'] and record_2['id'] in ['91', '92', '93']:
            #     logger.info(f"Distance between {record_1['id']} and {record_2['id']}: {distance_matrix[i, :stop]}")
            #     ids_of_interest[i] = (record_1['id'], record_2['id'])

        distance_matrix = self._compute_interaction_distances(distance_matrix)
        # for i, pair in ids_of_interest.items():
        #     logger.info(f"Distance between {pair[0]} and {pair[1]}: {distance_matrix[i, :]}")
        #     logger.info(numpy.sum(distance_matrix[i, :]))

        return distance_matrix

    def _compute_interaction_distances(self, primary_distance_matrix):
        distance_matrix = primary_distance_matrix

        current_column = self._derived_start
        for interaction, weight in zip(self._interaction_indices, self._interaction_weights):
            distance_matrix[:, current_column] =\
                numpy.prod(distance_matrix[:, interaction], axis=1)*weight
            current_column += 1

        missing_data = numpy.isnan(distance_matrix[:, :current_column])

        distance_matrix[:, :current_column][missing_data] = 0.5

        if self._missing_field_indices:
            distance_matrix[:, current_column:] =\
                1 - missing_data[:, self._missing_field_indices]

        return distance_matrix

    def check(self, record):
        """Check that a record has all the required fields.

        Args:
            record: (dict)
        """
        for field_comparator in self._field_comparators:
            field = field_comparator[0]
            if field not in record:
                raise ValueError("Records do not line up with data model. "
                                 "The field '%s' is in distances but not "
                                 "in a record" % field)


def typifyFields(fields):
    """Given the field definitions, compute list of predicates for each field.
    Args:
        fields: (list)(dict) a dictionary of field definitions as supplied in the ``fields.json``
            file, eg
            ::

                [
                    {'field': 'middle_name', 'variable name': 'middle_name', 'type': 'String'},
                    {'field': 'street_address', 'variable name': 'street_address',
                        'type': 'String', 'weight': 0.5}
                ]
    """
    primary_fields = []
    distance_matrix = []

    for definition in fields:
        try:
            field_type = definition['type']
        except TypeError:
            raise TypeError("Incorrect field specification: field "
                            "specifications are dictionaries that must "
                            "include a type definition, ex. "
                            "{'field' : 'Phone', type: 'String'}")
        except KeyError:
            raise KeyError("Missing field type: fields "
                           "specifications are dictionaries that must "
                           "include a type definition, ex. "
                           "{'field' : 'Phone', type: 'String'}")

        if field_type == 'Interaction':
            continue

        if field_type == 'FuzzyCategorical' and 'other fields' not in definition:
            definition['other fields'] = [d['field'] for d in fields
                                          if ('field' in d and
                                              d['field'] != definition['field'])]

        try:
            field_class = FIELD_CLASSES[field_type]
        except KeyError:
            raise KeyError("Field type %s not valid. Valid types include %s"
                           % (definition['type'], ', '.join(FIELD_CLASSES)))

        field_object = field_class(definition)
        # logger.info(f"dedupe.distances L193: field_object: {dir(field_object)}")
        # logger.info(f"dedupe.distances L193: field_object: {field_object.predicates}")
        primary_fields.append(field_object)

        if hasattr(field_object, 'higher_vars'):
            distance_matrix.extend(field_object.higher_vars)
        else:
            distance_matrix.append(field_object)

    return primary_fields, distance_matrix


def missing(distance_matrix):
    missing_variables = []
    for definition in distance_matrix[:]:
        if definition.has_missing:
            missing_variables.append(MissingDataType(definition.name))

    return missing_variables


def interactions(definitions, primary_fields):
    field_d = {field.name: field for field in primary_fields}
    interaction_class = InteractionType

    interactions = []

    for definition in definitions:
        if definition['type'] == 'Interaction':
            field = interaction_class(definition)
            field.expandInteractions(field_d)
            interactions.extend(field.higher_vars)

    return interactions


def missing_field_indices(variables):
    return [i for i, definition
            in enumerate(variables)
            if definition.has_missing]


def interaction_indices(variables):
    indices = []

    field_names = [field.name for field in variables]

    for definition in variables:
        if hasattr(definition, 'interaction_fields'):
            interaction_indices = []
            for interaction_field in definition.interaction_fields:
                interaction_indices.append(
                    field_names.index(interaction_field))
            indices.append(interaction_indices)

    return indices


def interaction_weights(variables):
    weights = []

    for definition in variables:
        if hasattr(definition, 'interaction_fields'):
            weights.append(definition.weight)

    return weights


def reduce_method(m):
    return (getattr, (m.__self__, m.__func__.__name__))


copyreg.pickle(types.MethodType, reduce_method)
