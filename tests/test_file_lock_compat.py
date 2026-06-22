def test_file_lock_exposes_fcntl_style_api(tmp_path):
    from ai_trader import file_lock

    lock_path = tmp_path / "state.lock"
    with lock_path.open("w") as lock_fd:
        file_lock.flock(lock_fd, file_lock.LOCK_EX)
        file_lock.flock(lock_fd, file_lock.LOCK_UN)
