# Copyright 2018 D-Wave Systems Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
import itertools
import unittest
import threading
import operator

import dimod

from hybrid.flow import (
    Branch, RacingBranches, ParallelBranches,
    ArgMin, Loop, Map, Reduce, Lambda, Unwind
)
from hybrid.core import State, States, Runnable, Present
from hybrid.utils import min_sample, max_sample
from hybrid.profiling import tictoc
from hybrid.exceptions import EndOfStream
from hybrid import traits


class TestBranch(unittest.TestCase):

    def test_empty(self):
        with self.assertRaises(ValueError):
            Branch()

    def test_composition(self):
        class A(Runnable):
            def next(self, state):
                return state.updated(x=state.x + 1)
        class B(Runnable):
            def next(self, state):
                return state.updated(x=state.x * 7)

        a, b = A(), B()
        s = State(x=1)

        b1 = Branch(components=(a, b))
        self.assertEqual(b1.components, (a, b))
        self.assertEqual(b1.run(s).result().x, (s.x + 1) * 7)

        b2 = b1 | b | a
        self.assertEqual(b2.components, (a, b, b, a))
        self.assertEqual(b2.run(s).result().x, (s.x + 1) * 7 * 7 + 1)

        with self.assertRaises(TypeError):
            b1 | 1

    def test_look_and_feel(self):
        class A(Runnable): pass
        class B(Runnable): pass

        b = A() | B()
        self.assertEqual(b.name, 'Branch')
        self.assertEqual(str(b), 'A | B')
        self.assertEqual(repr(b), 'Branch(components=(A(), B()))')
        self.assertEqual(tuple(b), b.components)
        self.assertIsInstance(b, Branch)
        self.assertIsInstance(b | b, Branch)

    def test_error_prop(self):
        class ErrorSilencer(Runnable):
            def next(self, state):
                return state
            def error(self, exc):
                return State(error=True)

        class Identity(Runnable):
            def next(self, state):
                return state

        branch = ErrorSilencer() | Identity()
        s1 = Present(exception=KeyError())
        s2 = branch.run(s1).result()

        self.assertEqual(s2.error, True)

    def test_stop(self):
        class Stoppable(Runnable):
            def init(self, state):
                self.stopped = False
            def next(self, state):
                return state
            def halt(self):
                self.stopped = True

        branch = Branch([Stoppable()])
        branch.run(State())
        branch.stop()

        self.assertTrue(next(iter(branch)).stopped)


class TestRacingBranches(unittest.TestCase):

    def test_look_and_feel(self):
        br = Runnable(), Runnable()
        rb = RacingBranches(*br)
        self.assertEqual(rb.name, 'RacingBranches')
        self.assertEqual(str(rb), '(Runnable) !! (Runnable)')
        self.assertEqual(repr(rb), 'RacingBranches(Runnable(), Runnable())')
        self.assertEqual(tuple(rb), br)

    def test_stopped(self):
        class Fast(Runnable):
            def next(self, state):
                time.sleep(0.1)
                return state.updated(x=state.x + 1)

        class Slow(Runnable):
            def init(self, state):
                self.time_to_stop = threading.Event()

            def next(self, state):
                self.time_to_stop.wait()
                return state.updated(x=state.x + 2)

            def halt(self):
                self.time_to_stop.set()

        # standard case
        rb = RacingBranches(Slow(), Fast(), Slow())
        res = rb.run(State(x=0)).result()
        self.assertEqual([s.x for s in res], [0, 2, 1, 2])

        # branches' outputs are of a different type that the inputs
        # (i.e. non-endomorphic racing branches)
        rb = RacingBranches(Slow(), Fast(), Slow(), endomorphic=False)
        res = rb.run(State(x=0)).result()
        self.assertEqual([s.x for s in res], [2, 1, 2])


class TestParallelBranches(unittest.TestCase):

    def test_look_and_feel(self):
        br = Runnable(), Runnable()
        pb = ParallelBranches(*br)
        self.assertEqual(pb.name, 'ParallelBranches')
        self.assertEqual(str(pb), '(Runnable) & (Runnable)')
        self.assertEqual(repr(pb), 'ParallelBranches(Runnable(), Runnable())')
        self.assertEqual(tuple(pb), br)

    def test_basic(self):
        class Fast(Runnable):
            def next(self, state):
                time.sleep(0.1)
                return state.updated(x=state.x + 1)

        class Slow(Runnable):
            def next(self, state):
                time.sleep(0.2)
                return state.updated(x=state.x + 2)

        # standard case (endomorphic; first output state is the input state)
        pb = ParallelBranches(Slow(), Fast(), Slow())
        res = pb.run(State(x=0)).result()
        self.assertEqual([s.x for s in res], [0, 2, 1, 2])

        # branches' outputs are of a different type that the inputs
        # (i.e. non-endomorphic branches)
        pb = ParallelBranches(Slow(), Fast(), Slow(), endomorphic=False)
        res = pb.run(State(x=0)).result()
        self.assertEqual([s.x for s in res], [2, 1, 2])

    def test_parallel_independent_execution(self):
        class Component(Runnable):
            def __init__(self, runtime):
                super(Component, self).__init__()
                self.runtime = runtime
            def next(self, state):
                time.sleep(self.runtime)
                return state

        # make sure all branches really run in parallel
        pb = ParallelBranches(
            Component(1), Component(1), Component(1), Component(1), Component(1))
        with tictoc() as tt:
            pb.run(State()).result()

        # total runtime has to be smaller that the sum of individual runtimes
        self.assertTrue(1 <= tt.dt <= 2)


class TestArgMin(unittest.TestCase):

    def test_look_and_feel(self):
        fold = ArgMin(key=False)
        self.assertEqual(fold.name, 'ArgMin')
        self.assertEqual(str(fold), '[]>')
        self.assertEqual(repr(fold), "ArgMin(key=False)")

        fold = ArgMin(key=min)
        self.assertEqual(repr(fold), "ArgMin(key=<built-in function min>)")

    def test_default_fold(self):
        bqm = dimod.BinaryQuadraticModel({'a': 1}, {}, 0, dimod.SPIN)
        states = States(
            State.from_sample(min_sample(bqm), bqm),    # energy: -1
            State.from_sample(max_sample(bqm), bqm),    # energy: +1
        )
        best = ArgMin().run(states).result()
        self.assertEqual(best.samples.first.energy, -1)

    def test_custom_fold(self):
        bqm = dimod.BinaryQuadraticModel({'a': 1}, {}, 0, dimod.SPIN)
        states = States(
            State.from_sample(min_sample(bqm), bqm),    # energy: -1
            State.from_sample(max_sample(bqm), bqm),    # energy: +1
        )
        fold = ArgMin(key=lambda s: -s.samples.first.energy)
        best = fold.run(states).result()
        self.assertEqual(best.samples.first.energy, 1)


class TestLoop(unittest.TestCase):

    def test_basic(self):
        class Inc(Runnable):
            def next(self, state):
                return state.updated(cnt=state.cnt + 1)

        it = Loop(Inc(), max_iter=100, convergence=100, key=lambda _: None)
        s = it.run(State(cnt=0)).result()

        self.assertEqual(s.cnt, 100)

    def test_validation(self):
        class simo(Runnable, traits.SIMO):
            def next(self, state):
                return States(state, state)

        with self.assertRaises(TypeError):
            Loop(simo())


class TestMap(unittest.TestCase):

    def test_isolated(self):
        class Inc(Runnable):
            def next(self, state):
                return state.updated(cnt=state.cnt + 1)

        states = States(State(cnt=1), State(cnt=2))
        result = Map(Inc()).run(states).result()

        self.assertEqual(result[0].cnt, states[0].cnt + 1)
        self.assertEqual(result[1].cnt, states[1].cnt + 1)

    def test_branch(self):
        class Inc(Runnable):
            def next(self, state):
                return state.updated(cnt=state.cnt + 1)

        states = States(State(cnt=1), State(cnt=2))
        branch = Map(Inc()) | ArgMin('cnt')
        result = branch.run(states).result()

        self.assertEqual(result.cnt, states[0].cnt + 1)

    def test_input_validation(self):
        with self.assertRaises(TypeError):
            Map(False)
        with self.assertRaises(TypeError):
            Map(lambda: None)
        with self.assertRaises(TypeError):
            Map(Runnable)
        self.assertIsInstance(Map(Runnable()), Runnable)


class TestReduce(unittest.TestCase):

    class Sum(Runnable, traits.MISO):
        def next(self, states):
            a, b = states
            return a.updated(val=a.val + b.val)

    def test_basic(self):
        states = States(State(val=1), State(val=2), State(val=3))
        result = Reduce(self.Sum()).run(states).result()

        self.assertIsInstance(result, State)
        self.assertEqual(result.val, 1+2+3)

    def test_initial_state(self):
        initial = State(val=10)
        states = States(State(val=1), State(val=2))
        result = Reduce(self.Sum(), initial_state=initial).run(states).result()

        self.assertEqual(result.val, 10+1+2)

    def test_unstructured_runnable(self):
        initial = State(val=10)
        states = States(State(val=2), State(val=3))

        multiply = Lambda(next=lambda self, s: s[0].updated(val=s[0].val * s[1].val))
        result = Reduce(multiply, initial_state=initial).run(states).result()

        self.assertEqual(result.val, 10*2*3)

    def test_input_validation(self):
        with self.assertRaises(TypeError):
            Reduce(False)
        with self.assertRaises(TypeError):
            Reduce(Runnable)
        self.assertIsInstance(Reduce(self.Sum()), Runnable)


class TestLambda(unittest.TestCase):

    def test_basic_runnable(self):
        runnable = Lambda(lambda _, s: s.updated(c=s.a * s.b))
        state = State(a=2, b=3)
        result = runnable.run(state).result()

        self.assertEqual(result.c, state.a * state.b)

    def test_error_and_init(self):
        runnable = Lambda(
            next=lambda self, state: state.updated(c=state.a * state.b),
            error=lambda self, exc: State(error=exc),
            init=lambda self, state: setattr(self, 'first', state.c)
        )

        # test init
        state = State(a=2, b=3, c=0)
        result = runnable.run(state).result()

        self.assertEqual(runnable.first, 0)
        self.assertEqual(result.c, state.a * state.b)

        # test error prop
        exc = ZeroDivisionError()
        result = runnable.run(Present(exception=exc)).result()

        self.assertEqual(result.error, exc)

    def test_map_lambda(self):
        states = States(State(cnt=1), State(cnt=2))
        result = Map(Lambda(lambda _, s: s.updated(cnt=s.cnt + 1))).run(states).result()

        self.assertEqual(result[0].cnt, states[0].cnt + 1)
        self.assertEqual(result[1].cnt, states[1].cnt + 1)

    def test_input_validation(self):
        with self.assertRaises(TypeError):
            Lambda(False)
        with self.assertRaises(TypeError):
            Lambda(lambda: None, False)
        with self.assertRaises(TypeError):
            Lambda(lambda: None, lambda: None, False)
        self.assertIsInstance(Lambda(lambda: None, lambda: None, lambda: None), Runnable)


class TestUnwind(unittest.TestCase):

    def test_basic(self):
        class Streamer(Runnable):
            def next(self, state):
                if state.cnt <= 0:
                    raise EndOfStream
                return state.updated(cnt=state.cnt - 1)

        r = Unwind(Streamer())
        states = r.run(State(cnt=3)).result()

        # states should contain 3 states with cnt=3..0
        self.assertEqual(len(states), 3)
        for idx, state in enumerate(states):
            self.assertEqual(state.cnt, 2-idx)

    def test_validation(self):
        class simo(Runnable, traits.SIMO):
            def next(self, state):
                return States(state, state)

        with self.assertRaises(TypeError):
            Unwind(simo())
