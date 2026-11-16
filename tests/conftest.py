# SPDX-License-Identifier: MPL-2.0
import os
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
import pytest
from PySide6.QtWidgets import QApplication
from vrun.routes import Point, build_loop


TEST_GEOMETRIES = {
    "rectangle": [(0, 0), (0, 0.001), (0.001, 0.001), (0.001, 0)],
    "triangle": [(1, 1), (1, 1.002), (1.002, 1.001)],
    "uneven": [(2, 2), (2, 2.001), (2.0006, 2.001), (2.001, 2.0005), (2.001, 2)],
}


@pytest.fixture(scope="session")
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def sample_route():
    return build_loop(Path("rectangle.gpx"), [Point(*coords) for coords in TEST_GEOMETRIES["rectangle"]])


@pytest.fixture(params=TEST_GEOMETRIES, ids=list(TEST_GEOMETRIES))
def varied_route(request):
    return build_loop(Path(f"{request.param}.gpx"), [Point(*coords) for coords in TEST_GEOMETRIES[request.param]])
