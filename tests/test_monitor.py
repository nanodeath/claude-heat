from claude_heat.config import Config
from claude_heat.monitor import Container, Proc, Sample, Spawned, cores_by_owner, flames

A = Spawned(100)
B = Spawned(200)
OUTSIDE = 1  # pid of a parent we don't track, like the Claude process itself


def test_survivor_counts_its_change():
    prev = Sample(now=1000, procs={(10, 5): Proc(A, 50, OUTSIDE)})
    cur = Sample(now=1200, procs={(10, 5): Proc(A, 250, OUTSIDE)})
    assert cores_by_owner(prev, cur) == {A: 1.0}


def test_new_process_counts_in_full():
    prev = Sample(now=1000, procs={})
    cur = Sample(now=1200, procs={(11, 1100): Proc(A, 100, OUTSIDE)})
    assert cores_by_owner(prev, cur) == {A: 0.5}


def test_process_first_seen_but_older_than_prev_sample_is_ignored():
    # Its lifetime total says nothing about the last interval
    prev = Sample(now=1000, procs={})
    cur = Sample(now=1200, procs={(12, 500): Proc(A, 9999, OUTSIDE)})
    assert cores_by_owner(prev, cur) == {}


def test_reaped_child_is_not_double_counted():
    # The child had used 100 ticks by prev and 50 more before exiting. Reaping
    # adds its whole 150 to the parent, but 100 of those were already counted.
    prev = Sample(
        now=1000,
        procs={(10, 5): Proc(A, 0, OUTSIDE), (20, 900): Proc(A, 100, 10)},
    )
    cur = Sample(now=1200, procs={(10, 5): Proc(A, 150, OUTSIDE)})
    assert cores_by_owner(prev, cur) == {A: 0.25}


def test_child_reaped_by_untracked_parent_takes_nothing_back():
    # Its final ticks are lost, but nothing was counted twice either
    prev = Sample(now=1000, procs={(20, 900): Proc(A, 100, OUTSIDE)})
    cur = Sample(now=1200, procs={})
    assert cores_by_owner(prev, cur) == {}


def test_grandchild_reaped_through_dead_parent():
    # Child and grandchild both exit; the grandparent absorbs both totals
    prev = Sample(
        now=1000,
        procs={
            (10, 5): Proc(A, 0, OUTSIDE),
            (20, 900): Proc(A, 40, 10),
            (30, 950): Proc(A, 60, 20),
        },
    )
    cur = Sample(now=1200, procs={(10, 5): Proc(A, 140, OUTSIDE)})
    assert cores_by_owner(prev, cur) == {A: 0.2}


def test_owners_are_kept_apart():
    prev = Sample(now=1000, procs={(10, 5): Proc(A, 0, OUTSIDE), (11, 5): Proc(B, 0, OUTSIDE)})
    cur = Sample(now=1200, procs={(10, 5): Proc(A, 200, OUTSIDE), (11, 5): Proc(B, 400, OUTSIDE)})
    assert cores_by_owner(prev, cur) == {A: 1.0, B: 2.0}


def test_container_owners_group_by_workdir():
    c1, c2 = Container("/p"), Container("/p")
    prev = Sample(now=1000, procs={(10, 5): Proc(c1, 0, OUTSIDE), (11, 5): Proc(c2, 0, OUTSIDE)})
    cur = Sample(now=1200, procs={(10, 5): Proc(c1, 100, OUTSIDE), (11, 5): Proc(c2, 100, OUTSIDE)})
    assert cores_by_owner(prev, cur) == {Container("/p"): 1.0}


def test_container_skipped_when_sessions_share_its_directory():
    one = {"/p": [(100, "/dev/pts/1")]}
    two = {"/p": [(100, "/dev/pts/1"), (200, "/dev/pts/2")]}
    assert Container("/p").session(one) == (100, "/dev/pts/1")
    assert Container("/p").session(two) is None
    assert Container("/q").session(one) is None


def test_flames_scale_with_thresholds():
    config = Config()
    assert flames(1.5, config) == "🔥"
    assert flames(4.0, config) == "🔥🔥"
    assert flames(12.0, config) == "🔥🔥🔥"


def test_hot_session_below_first_threshold_keeps_one_flame():
    assert flames(0.8, Config()) == "🔥"
