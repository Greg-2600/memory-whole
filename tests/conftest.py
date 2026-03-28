"""Shared test fixtures for Memory Whole."""

import sqlite3

import pytest

import db


@pytest.fixture()
def conn():
    """In-memory SQLite database with schema initialised."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_db(c)
    yield c
    c.close()
