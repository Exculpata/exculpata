"""Exact application requests and runtime tokenizer admission for the vLLM profile."""

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)
import hashlib
import json

from .workspace_store import WorkspaceProblem


def encode_request(request):
    return json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class RecordedDispatch:
    def __init__(self, repository, job, validate, admission):
        self.repository = repository
        self.job = job
        self.validate = validate
        self.admission = admission

    def _send(self, client, path, request, repair, budget=None):
        from .generation import _bounded_json_request
        # The same ordered object is persisted and handed to the existing JSON
        # transport serializer. Headers (including credentials) are never saved.
        wire = encode_request(request)
        request = json.loads(wire)
        manifest = dict(format=f"{LEGACY_STEM}-dispatch-v1", request=request,
            purpose='tokenization' if path == '/tokenize' else 'generation',
            serialized_request_utf8=wire.decode('utf-8'),
            serialized_utf8_sha256=hashlib.sha256(wire).hexdigest(), serialized_utf8_bytes=len(wire),
            normalization='question/history follow existing prompt policy; selected orientation preserved exactly',
            repair=repair, admission=self.admission,
            runtime=dict(profile='vllm-chat-tokenize', configured_model=client.model,
                         artifact_revision=None, runtime_version=None, per_response_attestation=False),
            budget=budget)
        self.validate()
        ordinal = self.repository.prepare(self.job, manifest)
        self.validate()
        self.repository.transition(self.job, ordinal, 'dispatch_attempted')
        try:
            response = _bounded_json_request(client.endpoint + path, request,
                                            timeout=client.timeout, headers=client._headers)
        except Exception:
            # Even a timeout may follow receipt by the runtime. Revocation or
            # cancellation may prevent the terminal update, leaving attempted.
            try:
                self.repository.transition(self.job, ordinal, 'transport_failed')
            except (WorkspaceProblem, KeyError):
                pass
            raise
        metadata = {}
        if path == '/tokenize':
            for key in ('count', 'max_model_len'):
                if type(response.get(key)) is int:
                    metadata[key] = response[key]
        else:
            for key in ('model', 'system_fingerprint'):
                if isinstance(response.get(key), str) and len(response[key]) <= 200:
                    metadata[key] = response[key]
            usage = response.get('usage')
            if isinstance(usage, dict):
                metadata['usage'] = {key: usage[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                                     if type(usage.get(key)) is int and usage[key] >= 0}
        self.repository.transition(self.job, ordinal, 'completed', metadata)
        return response

    def send(self, client, request, repair):
        from .generation import GenerationUnavailable
        # Pin the same chat-template controls on both endpoints: relying on
        # different adapter defaults would invalidate complete-request admission.
        request['add_generation_prompt'] = True
        request['add_special_tokens'] = False
        token_request = dict(model=request['model'], messages=request['messages'],
                             add_generation_prompt=request['add_generation_prompt'],
                             add_special_tokens=request['add_special_tokens'])
        if 'chat_template_kwargs' in request:
            token_request['chat_template_kwargs'] = request['chat_template_kwargs']
        try:
            tokens = self._send(client, '/tokenize', token_request, repair)
        except GenerationUnavailable as exc:
            raise WorkspaceProblem('Saved context requires the configured runtime chat tokenizer and context window. Tokenizer access is unavailable; turn context off or restore the supported runtime.') from exc
        count, window = tokens.get('count'), tokens.get('max_model_len')
        ids = tokens.get('tokens')
        if (type(count) is not int or count < 1 or type(window) is not int or window < 1 or
            not isinstance(ids, list) or len(ids) != count or any(type(i) is not int or i < 0 for i in ids)):
            raise WorkspaceProblem('The runtime did not establish a complete chat token count and window. Saved context was not dispatched for generation.')
        reserved, margin = request['max_tokens'], 256
        if count + reserved + margin > window:
            raise WorkspaceProblem('The complete request exceeds the runtime window with reserved output. Reduce context/history or use a new conversation; no prefix was supplied for generation.')
        request['truncate_prompt_tokens'] = None
        budget = dict(input_tokens=count, runtime_window=window, reserved_output_tokens=reserved,
                      safety_margin_tokens=margin, tokenizer='runtime /tokenize with chat template',
                      token_ids_sha256=hashlib.sha256(encode_request(ids)).hexdigest())
        response = self._send(client, '/v1/chat/completions', request, repair, budget)
        usage = response.get('usage')
        if not isinstance(usage, dict) or usage.get('prompt_tokens') != count:
            raise WorkspaceProblem('The runtime did not confirm the admitted input token count. Dispatch is recorded, but this answer cannot be accepted; review the runtime profile.')
        return response
