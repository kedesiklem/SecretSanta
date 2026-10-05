"""The solver against an exhaustive search: same optimum, never a hard rule broken."""
import itertools
import random
import unittest

from helpers import Santa


def brute_force(n, entries, single_cycle):
    """Best lexicographic violation vector over *all* valid assignments, or None if none exists."""
    units = {}
    for e in entries:
        if e.soft:
            key = ("b", e.bundle) if e.bundle is not None else ("e", id(e))
            units.setdefault(key, []).append(e)
    prios = sorted({es[0].priority for es in units.values()}, reverse=True)
    best = None
    for perm in itertools.permutations(range(n)):
        if any(perm[i] == i for i in range(n)):
            continue
        if single_cycle:
            seen, i = 0, 0
            while True:
                i = perm[i]
                seen += 1
                if i == 0:
                    break
            if seen != n:
                continue
        a = dict(enumerate(perm))
        if not all(Santa._satisfied(e, a) for e in entries if not e.soft):
            continue
        vec = tuple(sum(1 for es in units.values() if es[0].priority == p
                        and not all(Santa._satisfied(e, a) for e in es)) for p in prios)
        best = vec if best is None or vec < best else best
    return best


def achieved(entries, assignment):
    units = {}
    for e in entries:
        if e.soft:
            key = ("b", e.bundle) if e.bundle is not None else ("e", id(e))
            units.setdefault(key, []).append(e)
    prios = sorted({es[0].priority for es in units.values()}, reverse=True)
    return tuple(sum(1 for es in units.values() if es[0].priority == p
                     and not all(Santa._satisfied(e, assignment) for e in es)) for p in prios)


def random_entries(rng, n):
    entries, bundle = [], 0
    for _ in range(rng.randint(0, 9)):
        a, b = rng.sample(range(n), 2)
        kind = "force" if rng.random() < .15 else "forbid"
        soft = rng.random() < .6
        prio = rng.choice([10, 20, 30])
        if rng.random() < .35:      # a bundle of several pairs, given up together
            pairs = {(a, b), (b, a), tuple(rng.sample(range(n), 2))}
            for p in sorted(pairs):
                entries.append(Santa.Entry("forbid", p, prio, soft, f"bundle{bundle}", bundle))
            bundle += 1
        else:
            entries.append(Santa.Entry(kind, (a, b), prio, soft, f"single{len(entries)}", None))
    return entries


class SolverMatchesBruteForce(unittest.TestCase):
    def test_random_instances(self):
        rng = random.Random(2026)
        checked = infeasible = 0
        for case in range(70):
            n = rng.randint(3, 6)
            single = rng.random() < .3
            entries = random_entries(rng, n)
            want = brute_force(n, entries, single)
            got = Santa.solve(n, entries, single_cycle=single, time_limit=10, seed=case)
            with self.subTest(case=case, n=n, single_cycle=single):
                if want is None:
                    self.assertFalse(got.feasible)
                    infeasible += 1
                    continue
                self.assertTrue(got.feasible)
                a = got.assignment
                self.assertEqual(sorted(a), list(range(n)))
                self.assertEqual(sorted(a.values()), list(range(n)))
                self.assertTrue(all(a[i] != i for i in a))
                self.assertTrue(all(Santa._satisfied(e, a) for e in entries if not e.soft))
                self.assertEqual(achieved(entries, a), want)
                checked += 1
        self.assertGreater(checked, 30)           # the generator is not producing only dead ends
        self.assertGreater(infeasible, 0)

    def test_reported_violations_are_counts_per_source(self):
        # Alice must give to Bob (hard) while a soft rule forbids it: one violation, by source name.
        entries = [Santa.Entry("force", (0, 1), 100, False, "forced", None),
                   Santa.Entry("forbid", (0, 1), 10, True, "history 2025", None)]
        got = Santa.solve(3, entries, seed=1)
        self.assertEqual(got.violations, [("history 2025", 1)])

    def test_seed_makes_the_draw_reproducible_and_no_seed_does_not(self):
        a = Santa.solve(8, [], seed=42).assignment
        b = Santa.solve(8, [], seed=42).assignment
        self.assertEqual(a, b)
        runs = {tuple(sorted(Santa.solve(8, [], time_limit=5).assignment.items())) for _ in range(6)}
        self.assertGreater(len(runs), 1)


if __name__ == "__main__":
    unittest.main()
