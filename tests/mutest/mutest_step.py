"""Test that out-of-order cmd execution is supported by step"""
from munet.mutest.userapi import wait_step, section, step

section("Test the no-output arg in step")

step('r1', 'touch test-file1; sleep 1; mv test-file1 test-file2', output=False)

wait_step('r1', 'ls', 'test-file1', 'Saw test-file1',  2, 0.1)
wait_step('r1', 'ls', 'test-file2', 'Saw test-file2',  2, 0.1)
