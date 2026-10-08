
import os as _os
import tempfile as _tempfile

# Os testes nunca gravam logs na pasta real do usuário.
_os.environ.setdefault("MPM_LOG_DIR", _tempfile.mkdtemp(prefix="mpm-test-logs-"))
