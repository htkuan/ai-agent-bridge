// Commit message rules enforced on PRs (see .github/workflows/commitlint.yml).
// config-conventional requires lowercase Conventional Commit types, e.g.
//   feat: add discord adapter
//   fix(slack): release dedupe slot on error
//   feat!: drop the [slack] extra        (! marks a breaking change)
// These are exactly the types python-semantic-release parses to bump versions.
export default {
  extends: ["@commitlint/config-conventional"],
};
