# -*- coding: utf-8 eval: (blacken-mode 1) -*-
# SPDX-License-Identifier: GPL-2.0-or-later
#
# June 28 2023, Eric Kinzie <ekinzie@labn.net>
#
# Copyright 2023, LabN Consulting, L.L.C.
#
"Testing of unconnected interface."
import logging

import pytest

# All tests are coroutines
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "unet_perfunc", ["munet", "noinit", "noinit-noshell"], indirect=["unet_perfunc"]
)
async def test_unconnected_presence(unet_perfunc):
    unet = unet_perfunc
    rc, o, e = await unet.hosts["r1"].async_cmd_status(f"ip addr show dev unconnected")
    assert rc == 0
    assert o.find("mtu 9000") > -1
    assert o.find("inet 172.16.0.1/24") > -1


async def test_basic_ping(unet_perfunc):
    unet = unet_perfunc
    r1eth0 = unet.hosts["r1"].get_intf_addr("eth0").ip
    logging.debug("r1eth0 is %s", r1eth0)
    rc, o, e = await unet.hosts["r2"].async_cmd_status(
        f"ip ro add 172.16.0.0/24 via {r1eth0}"
    )
    assert rc == 0
    o = await unet.hosts["r2"].async_cmd_raises(f"ping -w1 -c1 172.16.0.1")
    logging.debug("ping r2 output: %s", o)
