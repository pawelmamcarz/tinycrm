import pytest

from apps.core.ai import ask_json, parse_json_response


def test_parse_json_with_noise():
    assert parse_json_response('Oto wynik: {"a": 1} koniec') == {"a": 1}


def test_parse_json_invalid():
    assert parse_json_response("brak json")["_error"] == "invalid_json"


def test_ask_json_without_key_returns_error():
    assert ask_json("sys", "prompt")["_error"] == "no_api_key"


@pytest.mark.django_db
def test_home_redirects_by_role(client, salesperson):
    client.force_login(salesperson)
    response = client.get("/")
    assert response.status_code == 302


@pytest.mark.django_db
def test_login_page(client):
    assert client.get("/logowanie/").status_code == 200
