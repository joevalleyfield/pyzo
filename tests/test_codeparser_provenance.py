from types import SimpleNamespace

import pyzo
from pyzo.core.codeparser import Job, Parser


class Documents:
    def __init__(self):
        self.token = SimpleNamespace(document_id="durable-document", revision=3)
        self.current = True
        self.identity_value = "durable-document"

    def cache_token(self, editor):
        return self.token

    def identity(self, editor):
        return self.identity_value

    def token_is_current(self, token):
        return self.current and token is self.token


class Editor:
    def id(self):
        return "/mutable/path.py"

    def toPlainText(self):
        return "answer = 42"


def with_documents(test):
    missing = object()
    previous = getattr(pyzo, "application", missing)
    documents = Documents()
    pyzo.application = SimpleNamespace(documents=documents)
    try:
        test(documents)
    finally:
        if previous is missing:
            del pyzo.application
        else:
            pyzo.application = previous


def test_parser_jobs_and_results_use_durable_document_token():
    def check(documents):
        parser = Parser()
        editor = Editor()

        parser.parseThis(editor)
        job = parser._requests["durable-document"]
        assert job.editorId == "durable-document"
        assert job.token is documents.token

        result = parser._analyze(job)
        assert result.editorId == "durable-document"
        assert result.token is documents.token
        assert result.isMatch(editor)

        documents.current = False
        assert not parser._job_is_current(job)
        assert not result.isMatch(editor)

    with_documents(check)


def test_parser_rejects_wrong_document_even_when_token_object_is_current():
    def check(documents):
        parser = Parser()
        editor = Editor()
        job = Job(editor.toPlainText(), "durable-document", documents.token)
        result = parser._analyze(job)

        documents.identity_value = "branched-document"
        assert not result.isMatch(editor)

    with_documents(check)


def test_retired_peer_view_never_falls_back_to_mutable_editor_id():
    def check(documents):
        parser = Parser()
        editor = Editor()
        documents.token = None

        parser.parseThis(editor)

        assert parser._requests == {}
        assert parser._getResult(editor) is None

    with_documents(check)


def test_legacy_parser_keeps_mutable_editor_id_compatibility():
    missing = object()
    previous = getattr(pyzo, "application", missing)
    pyzo.application = None
    try:
        parser = Parser()
        editor = Editor()
        parser.parseThis(editor)
        job = parser._requests["/mutable/path.py"]
        assert job.token is None
        assert parser._job_is_current(job)
        assert parser._analyze(job).isMatch(editor)
    finally:
        if previous is missing:
            del pyzo.application
        else:
            pyzo.application = previous
