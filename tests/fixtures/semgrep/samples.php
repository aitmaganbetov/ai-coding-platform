<?php
// Positive samples for the bundled Semgrep rules (test fixture; never executed).
function bad($input) {
    shell_exec($input);
    unserialize($input);
    DB::select("SELECT * FROM users WHERE id = " . $input);
}
