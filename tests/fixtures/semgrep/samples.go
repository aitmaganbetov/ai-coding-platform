// Positive sample (test fixture; never executed).
package samples

import "os/exec"

func Bad(input string) error {
	return exec.Command("sh", "-c", input).Run()
}
