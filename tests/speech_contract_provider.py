"""Fixed synthetic speech API replies for the local TLS contract check."""
import hashlib
import json
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import subprocess
import tempfile


ASR_PATH = "/v1/audio/transcriptions"
ALIGNMENT_PATH = "/v1/audio/alignments"
SPEAKER_PATH = "/v1/audio/speaker-labels"
TRANSLATION_PATH = "/v1/translations"
MODELS = {ASR_PATH: "synthetic-asr", ALIGNMENT_PATH: "synthetic-alignment",
          SPEAKER_PATH: "synthetic-diarization", TRANSLATION_PATH: "synthetic-translation"}
RECORDING_SHA256 = "325e73ecfff901a056d8e08ea55d7222e77ace5b2058459f0d25ae2b344ac90b"
ENGLISH = "The blue marker is beside the empty box. This is a generated recording for a software test."
SPANISH = "Grabación de prueba."


def contract_reply(path, fields):
    if path not in MODELS or fields.get("model") != MODELS[path]:
        raise ValueError("Unknown fixture contract")
    if path != TRANSLATION_PATH:
        if hashlib.sha256(fields["file"]).hexdigest() != RECORDING_SHA256:
            raise ValueError("Expected packaged recording")
    if path == ASR_PATH:
        if fields.get("response_format") != "verbose_json" or fields.get("timestamp_granularities[]") != "segment":
            raise ValueError("Expected timed ASR request")
        return {"text": SPANISH, "language": "es", "duration": 5.5,
                "segments": [{"start": 0.0, "end": 5.5, "text": SPANISH}]}

    segments = fields["segments"] if path == TRANSLATION_PATH else json.loads(fields["segments"])
    if not isinstance(segments, list) or len(segments) != 1 or not isinstance(segments[0], dict):
        raise ValueError("Expected one synthetic segment")
    segment = segments[0]
    segment_id = segment["id"]
    if not isinstance(segment_id, str) or not 0 < len(segment_id) <= 200:
        raise ValueError("Expected a segment ID")
    result = {"id": segment_id}
    if path == TRANSLATION_PATH:
        if (set(fields) != {"model", "source_language", "target_language", "segments"}
                or set(segment) != {"id", "text"} or segment["text"] != SPANISH
                or fields["source_language"] != "es" or fields["target_language"] != "en"):
            raise ValueError("Expected synthetic translation input")
        result["text"] = "Test recording."
    else:
        start, end = segment["start"], segment["end"]
        if type(start) not in (int, float) or type(end) not in (int, float) or not 0 <= start < end <= 5.52:
            raise ValueError("Expected times within the synthetic recording")
        if path == SPEAKER_PATH:
            if set(segment) != {"id", "start", "end"}:
                raise ValueError("Unexpected speaker labeling fields")
            result["speaker"] = "SPEAKER_01"
        else:
            if (fields["language"], segment["text"]) not in {("en", ENGLISH), ("es", SPANISH)}:
                raise ValueError("Expected synthetic alignment input")
            tokens = segment["text"].split()
            margin = (end - start) / 50
            start += margin
            end -= margin
            step = (end - start) / len(tokens)
            result["words"] = [{"word": word, "start": round(start + i * step, 3),
                                "end": round(start + (i + 1) * step, 3)} for i, word in enumerate(tokens)]
    return {"segments": [result]}


class SpeechContractHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 256 * 1024:
                raise ValueError("Expected bounded input")
            body = self.rfile.read(length)
            if self.path == TRANSLATION_PATH and self.headers.get_content_type() == "application/json":
                fields = json.loads(body)
            elif self.path in (ASR_PATH, ALIGNMENT_PATH, SPEAKER_PATH) and self.headers.get_content_type() == "multipart/form-data":
                header = f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode()
                message = BytesParser(policy=policy.default).parsebytes(header + body)
                fields = {}
                for part in message.iter_parts():
                    name = part.get_param("name", header="Content-Disposition")
                    if name in fields:
                        raise ValueError("Duplicate field")
                    data = part.get_payload(decode=True)
                    fields[name] = data if name == "file" else data.decode("utf-8")
            else:
                raise ValueError("Unknown fixture request")
            if not isinstance(fields, dict):
                raise ValueError("Expected request fields")
            response, status = contract_reply(self.path, fields), 200
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            response, status = {"error": "Expected the synthetic contract fixture"}, 400
        encoded = json.dumps(response).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)  # fixture-write: bounded synthetic response


def main():
    hosts = [role + ".example.test" for role in ("asr", "alignment", "translation", "diarization")]
    certificate = Path("/tmp/speech-contract-ca.pem")
    with tempfile.TemporaryDirectory() as private:
        key = Path(private) / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1",
                        "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=speech.example.test",
                        "-addext", "subjectAltName=" + ",".join("DNS:" + host for host in hosts)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        with ThreadingHTTPServer(("0.0.0.0", 8443), SpeechContractHandler) as server:
            server.socket = context.wrap_socket(server.socket, server_side=True)
            server.serve_forever()


if __name__ == "__main__":
    main()
