"""Wspólne fixtury: organizacja, druga organizacja (testy izolacji) i użytkownicy per rola."""
import pytest

from apps.core.models import Organization
from apps.users.models import Role, User


@pytest.fixture
def org(db):
    return Organization.objects.create(name="MedDystrybucja", slug="med")


@pytest.fixture
def other_org(db):
    return Organization.objects.create(name="Inna firma", slug="inna")


def _user(org, username, role):
    return User.objects.create_user(username=username, password="haslo-testowe-123", organization=org, role=role,
                                    first_name=username.capitalize())


@pytest.fixture
def salesperson(org):
    return _user(org, "jan", Role.HANDLOWIEC)


@pytest.fixture
def office_user(org):
    return _user(org, "ola", Role.BIURO)


@pytest.fixture
def service_user(org):
    return _user(org, "marek", Role.SERWIS)


@pytest.fixture
def boss(org):
    return _user(org, "szef", Role.SZEF)


@pytest.fixture
def other_org_user(other_org):
    return _user(other_org, "obcy", Role.BIURO)
