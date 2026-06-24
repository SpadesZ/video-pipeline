from pathlib import Path
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.stages.packaging_generator import write_upload_package
import tempfile
import shutil

def test_packaging_writer():
    print("Running packaging generator unit test...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Test Case 1: Fallback case (video_packaging is None)
        artifact = ProductionArtifact(
            project_id="test_fallback",
            title="My Fallback Video Title"
        )
        
        filepath = write_upload_package(tmp_path, artifact)
        content = filepath.read_text(encoding="utf-8")
        
        assert "# My Fallback Video Title" in content
        assert "Candidate Titles" in content
        assert "Description Draft" in content
        print("[Pass] Fallback packaging writer test passed.")
        
        # Test Case 2: Optimized case (video_packaging is set)
        artifact_opt = ProductionArtifact(
            project_id="test_opt",
            title="My Opt Video Title",
            video_packaging={
                "candidate_titles": ["Title A", "Title B"],
                "selected_title": "Title A",
                "thumbnail_prompt": "Draw a beautiful sunset over SaaS dashboard.",
                "description": "This is a detailed optimized description for SEO.",
                "tags": ["saas", "money"]
            }
        )
        
        filepath_opt = write_upload_package(tmp_path, artifact_opt)
        content_opt = filepath_opt.read_text(encoding="utf-8")
        
        assert "Title A (Optimized Packaging)" in content_opt
        assert "Thumbnail Prompt" in content_opt
        assert "Draw a beautiful sunset over SaaS dashboard." in content_opt
        assert "This is a detailed optimized description for SEO." in content_opt
        assert "saas, money" in content_opt
        print("[Pass] Optimized packaging writer test passed.")
        
    print("ALL PACKAGING GENERATOR TESTS PASSED!")

if __name__ == "__main__":
    test_packaging_writer()
