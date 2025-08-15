import os, random, time, pytest

def test_random_failure():
    # 40% fail rate simulates nondeterministic timing/network
    assert random.random() > 0.4

def test_sleepy():
    # Pretend we depend on a slow service; flake if scheduler jitter hits
    time.sleep(0.01)
    assert True

order_state = []

@pytest.mark.order(2)
def test_order_dep_b():
    # Fails if A hasn't run (order-sensitive test)
    assert "A" in order_state

@pytest.mark.order(1)
def test_order_dep_a():
    order_state.append("A")
    assert True
