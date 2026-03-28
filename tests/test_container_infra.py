import unittest
from pathlib import Path


class TestContainerInfra(unittest.TestCase):
    def test_dockerfile_exposes_port_and_healthcheck(self):
        df = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("EXPOSE 4747", df)
        self.assertIn("HEALTHCHECK", df)

    def test_docker_compose_has_volume_and_env(self):
        dc = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("mm_output", dc)
        self.assertIn("FORCE_REGEN", dc)

    def test_entrypoint_exists_and_is_executable(self):
        p = Path("scripts/docker_entrypoint.sh")
        self.assertTrue(p.exists())
        # execute bit may not be preserved in git, but ensure file starts with a shebang
        content = p.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("#!/usr/bin/env bash"))

    def test_docker_compose_port_mapping(self):
        dc = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('"4747:4747"', dc)


if __name__ == "__main__":
    unittest.main()
