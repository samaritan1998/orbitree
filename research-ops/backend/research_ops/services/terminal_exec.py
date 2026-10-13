"""Establish a controlling PTY in a fresh process, without preexec_fn in the API."""

import fcntl
import os
import sys
import termios

if __name__ == "__main__":
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execv(sys.argv[1], sys.argv[1:])
