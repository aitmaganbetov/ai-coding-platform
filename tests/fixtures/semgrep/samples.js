// Positive samples for the bundled Semgrep rules (test fixture; never executed).
const child_process = require("child_process");

function bad(input) {
  eval(input);
  child_process.exec(input);
}
module.exports = bad;
