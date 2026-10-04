from claude_heat.config import Config
from claude_heat.monitor import (
    Container,
    Heat,
    Proc,
    Sample,
    Spawned,
    cores_by_owner,
    flame_level,
)

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


def test_flame_level_counts_thresholds_reached():
    config = Config()
    assert flame_level(1.0, False, config) == 0
    assert flame_level(1.5, False, config) == 1
    assert flame_level(4.0, False, config) == 2
    assert flame_level(12.0, False, config) == 3


def test_hot_session_keeps_one_flame_until_below_cool_cores():
    config = Config()
    assert flame_level(0.8, True, config) == 1
    assert flame_level(0.4, True, config) == 0


def heat(**overrides) -> Heat:
    return Heat(Config(**{"interval": 2.0, "ramp_down": 10.0, **overrides}))


def test_heat_needs_a_full_window_to_turn_hot():
    h = heat()
    assert h.update(9.0, window_full=False) == 0
    assert h.update(9.0, window_full=True) == 3


def test_heat_rises_at_once():
    h = heat()
    assert h.update(2.0, True) == 1
    assert h.update(9.0, True) == 3


def test_heat_holds_its_peak_for_ramp_down():
    # ramp_down 10s at 2s samples: a level shows for 5 samples, counting the
    # one that earned it
    h = heat()
    h.update(9.0, True)
    assert [h.update(0.0, True) for _ in range(5)] == [3, 3, 3, 3, 0]


def test_heat_drops_to_the_lower_level_once_the_peak_expires():
    h = heat()
    h.update(9.0, True)
    assert [h.update(2.0, True) for _ in range(5)] == [3, 3, 3, 3, 1]


def test_brief_dip_does_not_blink():
    h = heat()
    shown = [h.update(avg, True) for avg in [5.0, 0.0, 0.0, 5.0, 0.0, 5.0]]
    assert shown == [2, 2, 2, 2, 2, 2]


def test_zero_ramp_down_follows_load_directly():
    h = heat(ramp_down=0.0)
    assert [h.update(avg, True) for avg in [9.0, 2.0, 0.0]] == [3, 1, 0]
