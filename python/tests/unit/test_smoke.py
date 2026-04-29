import pytest
from fastactor import Runtime
from fastactor.otp import (
    Call,
    Cast,
    Continue,
    Down,
    DynamicSupervisor,
    GenServer,
    Registry,
)


@pytest.mark.anyio
async def test_runtime_starts_and_stops() -> None:
    async with Runtime():
        pass


def test_otp_primitives_available() -> None:
    for symbol in (GenServer, DynamicSupervisor, Registry, Continue, Call, Cast, Down):
        assert symbol is not None
