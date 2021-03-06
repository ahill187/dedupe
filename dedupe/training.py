#!/usr/bin/python
# -*- coding: utf-8 -*-

# provides functions for selecting a sample of training data

import itertools
import logging
import collections
import functools

from collections.abc import Mapping

from . import blocking, predicates, core

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
# -*- coding: future_fstrings -*-


class BlockLearner(object):
    def learn(self, matches, recall):
        """
        Takes in a set of training pairs and predicates and tries to find
        a good set of blocking rules. Returns a subset of the initial
        predicates which represent the minimum predicates required to
        cover the training data.

            :comparison_count: (dict) {
                key: (dedupe.predicates class)
                value: (float)
            }

        Args:
            :matches: (list)[tuple][dict] list of pairs of records which
                are labelled as matches (duplicates) from the active labelling
                [
                    (record1, record2)
                ]
            :recall: (float) number between 0 and 1, minimum fraction of the
                active labelling training data that must be covered by
                the blocking predicate rules

        Returns:
            :final_predicates: (tuple)[tuple] tuple of final predicate rules
                (
                    (predicate1, predicate2, ... predicateN),
                    (predicate3, predicate4),
                    ...
                )
                Each element in final_predicates consists of a tuple of
                N predicates.
        """
        comparison_count = self.comparison_count
        logger.debug("training.BlockLearner.learn")
        logger.debug(f"Number of initial predicates: {len(self.blocker.predicates)}")
        # logger.debug(self.blocker.predicates)
        dupe_cover = Cover(self.blocker.predicates, matches)
        dupe_cover.compound(compound_length=self.compound_length)
        dupe_cover.intersection_update(comparison_count)

        dupe_cover.dominators(cost=comparison_count)

        coverable_dupes = set.union(*dupe_cover.values())
        # logger.debug(dupe_cover.values())
        uncoverable_dupes = [pair for i, pair in enumerate(matches)
                             if i not in coverable_dupes]
        logger.debug(f"Uncoverable dupes: {uncoverable_dupes}")
        epsilon = int((1.0 - recall) * len(matches))
        logger.debug(f"Recall: {recall}, epsilon: {epsilon}")

        if len(uncoverable_dupes) > epsilon:
            logger.warning(OUT_OF_PREDICATES_WARNING)
            logger.debug(uncoverable_dupes)
            epsilon = 0
        else:
            epsilon -= len(uncoverable_dupes)

        for pred in dupe_cover:
            pred.count = comparison_count[pred]
        logger.debug(f"Target: {len(coverable_dupes)-epsilon}")
        searcher = BranchBound(target=len(coverable_dupes) - epsilon,
                               max_calls=2500)
        final_predicates = searcher.search(dupe_cover)
        logger.info('Final predicate set:')
        for predicate in final_predicates:
            logger.info(predicate)
        logger.debug(f"Final predicates: {final_predicates}")
        logger.debug(f"Number of final predicate rules: {len(final_predicates)}")
        return final_predicates

    def compound(self, simple_predicates, compound_length):
        simple_predicates = sorted(simple_predicates, key=str)

        for pred in simple_predicates:
            yield pred

        CP = predicates.CompoundPredicate

        for i in range(2, compound_length + 1):
            compound_predicates = itertools.combinations(simple_predicates, i)
            for pred_a, pred_b in compound_predicates:
                if pred_a.compounds_with(pred_b) and pred_b.compounds_with(pred_a):
                    yield CP((pred_a, pred_b))

    def comparisons(self, predicates, simple_cover):
        """

        Args:
            simple_cover: (dict) {
                key: (dedupe.predicates class)
                value: (dedupe.training.Counter)
                }
            predicates: (generator)[dedupe.predicates class]

        Returns:
            comparison_count: (dict) {
                key: (dedupe.predicates class)
                value: (float)
                }
        """
        logger.debug("training.BlockLearner.comparisons")
        compounder = self.Compounder(simple_cover)
        comparison_count = {}

        for pred in predicates:
            if len(pred) > 1:
                estimate = self.estimate(compounder(pred))
            else:
                estimate = self.estimate(simple_cover[pred])

            comparison_count[pred] = estimate
        logger.debug(f"Comparison count: {len(comparison_count)}")
        return comparison_count

    class Compounder(object):
        def __init__(self, cover):
            self.cover = cover
            self._cached_predicate = None
            self._cached_cover = None

        def __call__(self, compound_predicate):
            a, b = compound_predicate[:-1], compound_predicate[-1]

            if len(a) > 1:
                if a == self._cached_predicate:
                    a_cover = self._cached_cover
                else:
                    a_cover = self._cached_cover = self(a)
                    self._cached_predicate = a
            else:
                a, = a
                a_cover = self.cover[a]

            return a_cover * self.cover[b]


class DedupeBlockLearner(BlockLearner):

    def __init__(self, predicates, sampled_records, data):
        """
        simple_cover: (dict) subset of the predicates list
            {
                key: (dedupe.predicates class)
                value: (dedupe.training.Counter)
            }
        compound_predicates: (generator) given the compound_length,
            this combines the predicates from simple_cover into
            combinations.
            Let n = len(simple_cover)
                k = compound_length
                L = number of compound_predicates
            Then L = n C k = n! / (n-k)!k!

        Args:
            predicates: (set)[dudupe.predicates class]
        """

        logger.debug("Initializing training.DedupeBlockLearner")
        self.compound_length = 2

        N = sampled_records.original_length
        N_s = len(sampled_records)

        self.r = (N * (N - 1)) / (N_s * (N_s - 1))

        self.blocker = blocking.Fingerprinter(predicates)
        self.blocker.index_all(data)

        simple_cover = self.coveredPairs(self.blocker, sampled_records)
        compound_predicates = self.compound(simple_cover, self.compound_length)
        self.comparison_count = self.comparisons(compound_predicates,
                                                 simple_cover)

    @staticmethod
    def coveredPairs(blocker, records):
        """

        For each field, there are one or more predicates. A predicate is a class
        defined in dedupe.predicates.py. A predicate is defined by the field
        it is associated with, and the predicate type. A predicate is callable
        (see the __call__ function).

        Pseudo-Algorithm:

            For each predicate, loop through the records list.
            Call the predicate function on each record.

        Args:
            :blocker: (blocking.Fingerprinter)
            :records: (dict)[dict] Records dictionary

        Returns:
            :cover: (dict) {
                key: (dedupe.predicates class)
                value: (dedupe.training.Counter)
            }
        """
        cover = {}

        pair_enumerator = core.Enumerator()
        n_records = len(records)
        # logger.debug("training.DedupeBlockLearner.coveredPairs")
        # logger.debug(len(blocker.predicates))
        for predicate in blocker.predicates:
            # logger.debug(predicate)
            pred_cover = collections.defaultdict(set)
            for id, record in records.items():
                blocks = predicate(record)
                for block in blocks:
                    pred_cover[block].add(id)

            if not pred_cover:
                continue

            max_cover = max(len(v) for v in pred_cover.values())
            if max_cover == n_records:
                continue

            pairs = (pair_enumerator[pair]
                     for block in pred_cover.values()
                     for pair in itertools.combinations(sorted(block), 2))
            cover[predicate] = Counter(pairs)
            # logger.debug(cover[predicate])
        # logger.debug(len(cover))
        return cover

    def estimate(self, comparisons):
        # Result due to Stefano Allesina and Jacopo Grilli,
        # details forthcoming
        #
        # This estimates the total number of comparisons a blocking
        # rule will produce.
        #
        # While it is true that if we block together records 1 and 2 together
        # N times we have to pay the overhead of that blocking and
        # and there is some cost to each one of those N comparisons,
        # we are using a redundant-free scheme so we only make one
        # truly expensive computation for every record pair.
        #
        # So, how can we estimate how many expensive comparison a
        # predicate will lead to? In other words, how many unique record
        # pairs will be covered by a predicate?

        return self.r * comparisons.total


class RecordLinkBlockLearner(BlockLearner):

    def __init__(self, predicates, sampled_records_1, sampled_records_2, data_2):

        compound_length = 2

        r_a = ((sampled_records_1.original_length) /
               len(sampled_records_1))
        r_b = ((sampled_records_2.original_length) /
               len(sampled_records_2))

        self.r = r_a * r_b

        self.blocker = blocking.Fingerprinter(predicates)
        self.blocker.index_all(data_2)

        simple_cover = self.coveredPairs(self.blocker,
                                         sampled_records_1,
                                         sampled_records_2)
        compound_predicates = self.compound(simple_cover, compound_length)

        self.comparison_count = self.comparisons(compound_predicates,
                                                 simple_cover)

    def coveredPairs(self, blocker, records_1, records_2):
        cover = {}

        pair_enumerator = core.Enumerator()

        for predicate in blocker.predicates:
            cover[predicate] = collections.defaultdict(lambda: (set(), set()))
            for id, record in records_2.items():
                blocks = predicate(record, target=True)
                for block in blocks:
                    cover[predicate][block][1].add(id)

            current_blocks = set(cover[predicate])
            for id, record in records_1.items():
                blocks = set(predicate(record))
                for block in blocks & current_blocks:
                    cover[predicate][block][0].add(id)

        for predicate, blocks in cover.items():
            pairs = {pair_enumerator[pair]
                     for A, B in blocks.values()
                     for pair in itertools.product(A, B)}
            cover[predicate] = Counter(pairs)

        return cover

    def estimate(self, comparisons):
        # For record pairs we only compare unique comparisons.
        #
        # I have no real idea of how to estimate the total number
        # of unique comparisons. Maybe the way to think about this
        # as the intersection of random multisets?
        #
        # In any case, here's the estimator we are using now.
        return self.r * comparisons.total


class BranchBound(object):
    def __init__(self, target, max_calls):
        """
        Args:
            :target: (float) desired number of active label training
                record matches to be covered by the predicate rules
                (computed from recall)
            :max_calls: (int) maximum number of iterations of the search
                function recursion
        """
        self.calls = max_calls
        self.target = target
        self.cheapest_score = float('inf')
        self.original_cover = None

    def search(self, candidates, partial=()):
        # logger.debug("training.BranchBound.search")
        if self.calls <= 0:
            return self.cheapest

        if self.original_cover is None:
            self.original_cover = candidates.copy()
            self.cheapest = candidates

        self.calls -= 1

        covered = self.covered(partial)
        score = self.score(partial)
        if covered >= self.target:
            logger.debug(f"""Number covered >= desired number covered,
                            covered={covered}, target={self.target},
                            score={score}
                            """)
            if score < self.cheapest_score:
                logger.debug(f'Candidates: {partial}')
                self.cheapest = partial
                self.cheapest_score = score

        else:
            window = self.cheapest_score - score
            # logger.debug(f'Cheapest score: {self.cheapest_score}')
            # logger.debug(f'Score: {score}')

            candidates = {p: cover
                          for p, cover in candidates.items()
                          if p.count < window}
            # logger.debug(f"candidates: {candidates}")
            reachable = self.reachable(candidates) + covered

            if candidates and reachable >= self.target:

                order_by = functools.partial(self.order_by, candidates)

                best = max(candidates, key=order_by)

                remaining = self.uncovered_by(candidates,
                                              candidates[best])
                self.search(remaining, partial + (best,))
                del remaining

                reduced = self.remove_dominated(candidates, best)
                self.search(reduced, partial)
                del reduced

        # logger.debug(f"Cheapest final: {self.cheapest}")
        return self.cheapest

    @staticmethod
    def order_by(candidates, p):
        return (len(candidates[p]), -p.count)

    @staticmethod
    def score(partial):
        """
        Args:
            :partial: (tuple)[predicates.CompoundPredicate]
        """
        for p in partial:
            pass
            # logger.debug(f"p: {p}")
            # logger.debug(type(p))
        return sum(p.count for p in partial)

    def covered(self, partial):
        if partial:
            return len(set.union(*(self.original_cover[p]
                                   for p in partial)))
        else:
            return 0

    @staticmethod
    def reachable(dupe_cover):
        if dupe_cover:
            return len(set.union(*dupe_cover.values()))
        else:
            return 0

    @staticmethod
    def remove_dominated(coverage, dominator):
        dominant_cover = coverage[dominator]

        for pred, cover in coverage.copy().items():
            if (dominator.count <= pred.count and
                    dominant_cover >= cover):
                del coverage[pred]

        return coverage

    @staticmethod
    def uncovered_by(coverage, covered):
        remaining = {}
        for predicate, uncovered in coverage.items():
            still_uncovered = uncovered - covered
            if still_uncovered:
                remaining[predicate] = still_uncovered

        return remaining


class Counter(object):
    def __init__(self, iterable):
        if isinstance(iterable, Mapping):
            self._d = iterable
        else:
            d = collections.defaultdict(int)
            for elem in iterable:
                d[elem] += 1
            self._d = d

        self.total = sum(self._d.values())

    def __le__(self, other):
        return (self._d.keys() <= other._d.keys() and
                self.total <= other.total)

    def __eq__(self, other):
        return self._d == other._d

    def __len__(self):
        return len(self._d)

    def __mul__(self, other):

        if len(self) <= len(other):
            smaller, larger = self._d, other._d
        else:
            smaller, larger = other._d, self._d

        # it's meaningfully faster to check in the key dictview
        # of 'larger' than in the dict directly
        larger_keys = larger.keys()

        common = {k: v * larger[k]
                  for k, v in smaller.items()
                  if k in larger_keys}

        return Counter(common)


class Cover(object):
    def __init__(self, *args):
        if len(args) == 1:
            self._d, = args
        else:
            self._d = {}
            predicates, pairs = args
            self._cover(predicates, pairs)

    def __repr__(self):
        return 'Cover:' + str(self._d.keys())

    def _cover(self, predicates, pairs):
        for predicate in predicates:
            coverage = {i for i, (record_1, record_2)
                        in enumerate(pairs)
                        if (set(predicate(record_1)) &
                            set(predicate(record_2, target=True)))}
            if coverage:
                self._d[predicate] = coverage

    def compound(self, compound_length):
        simple_predicates = sorted(self._d, key=str)
        CP = predicates.CompoundPredicate

        for i in range(2, compound_length + 1):
            compound_predicates = itertools.combinations(simple_predicates, i)

            for compound_predicate in compound_predicates:
                a, b = compound_predicate[:-1], compound_predicate[-1]
                if len(a) == 1:
                    a = a[0]

                if a in self._d:
                    compound_cover = self._d[a] & self._d[b]
                    if compound_cover:
                        self._d[CP(compound_predicate)] = compound_cover

    def dominators(self, cost):
        """
        candidate_match: list of active label training ids which are covered
            by the compound predicate rule
        candidate_cost: (float) computational cost of this predicate

        Pseudo-Algorithm
        1. Loop through list of predicates
            a. Loop through remainder of list of predicates
                - If a better or equally good match later in the list
                    is found, continue to the next predicate in outer
                    loop
                - A better match is one with lower computational cost
                    and one which covers more records in training data
                - If not, add the predicate to the dominants list
        """
        logger.debug("training.Cover.dominators")
        def sort_key(x):
            return (-cost[x], len(self._d[x]))

        ordered_predicates = sorted(self._d, key=sort_key)
        dominants = {}
        for i, candidate in enumerate(ordered_predicates):
            candidate_match = self._d[candidate]
            candidate_cost = cost[candidate]
            for pred in ordered_predicates[(i + 1):]:
                other_match = self._d[pred]
                other_cost = cost[pred]
                better_or_equal = (other_match >= candidate_match and
                                   other_cost <= candidate_cost)
                if better_or_equal:
                    break
            else:
                dominants[candidate] = candidate_match
        # logger.debug(f"dominants: {dominants}")
        self._d = dominants

    def __iter__(self):
        return iter(self._d)

    def keys(self):
        return self._d.keys()

    def values(self):
        return self._d.values()

    def items(self):
        return self._d.items()

    def __getitem__(self, k):
        return self._d[k]

    def copy(self):
        return Cover(self._d.copy())

    def update(self, *args, **kwargs):
        self._d.update(*args, **kwargs)

    def __eq__(self, other):
        return self._d == other._d

    def intersection_update(self, other):
        self._d = {k: self._d[k] for k in set(self._d) & set(other)}


OUT_OF_PREDICATES_WARNING = "Ran out of predicates: Dedupe tries to find blocking rules that will work well with your data. Sometimes it can't find great ones, and you'll get this warning. It means that there are some pairs of true records that dedupe may never compare. If you are getting bad results, try increasing the `max_comparison` argument to the train method"  # noqa: E501
