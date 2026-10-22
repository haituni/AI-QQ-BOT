"""Nonblocking advisory locks shared by launcher and server on both platforms."""
import os
if os.name == 'nt':
    import msvcrt
    LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4
    def flock(handle, operation):
        handle.seek(0)
        if operation & LOCK_UN:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BlockingIOError('Project is already running') from error
else:
    from fcntl import flock, LOCK_EX, LOCK_NB, LOCK_UN
