import os
import asyncio
import httpx
import sys

async def test_api_closed_loop():
    # Detect if running inside docker container or on the host machine
    if os.path.exists("/.dockerenv") or os.getenv("CELERY_BROKER_URL"):
        base_url = "http://localhost:8000"
    else:
        api_port = os.getenv("API_PORT", "8010")
        base_url = f"http://localhost:{api_port}"
    
    print("=== STARTING CLOSED LOOP PIPELINE INTEGRATION TEST ===")
    
    # 1. Test Niche Trend Research API
    print("\n[Step 1] Triggering LAVA Trend Research...")
    payload = {"topic_prompt": "AI SaaS变现"}
    async with httpx.AsyncClient(timeout=30) as client:
        # Simulate form post
        response = await client.post(f"{base_url}/projects/trend-research", data=payload)
        if response.status_code != 200:
            print(f"FAILED Step 1: Status code {response.status_code}")
            sys.exit(1)
        print("SUCCESS: Trend research list generated and HTML rendered.")
        
    # 2. Test Project Creation from Trend Angle
    print("\n[Step 2] Creating project and drafting script via LAVA Content Creator...")
    create_payload = {
        "title": "AI SaaS 变现之半自动生产线",
        "primary_keyword": "AI SaaS变现",
        "angle": "别再全自动！为何半自动 AI SaaS 变现才是唯一能做長期的產線"
    }
    async with httpx.AsyncClient(timeout=180) as client:
        # FastAPI Form post
        response = await client.post(f"{base_url}/projects/trend-research/create", data=create_payload, follow_redirects=False)
        # It should return a 303 Redirect to the project view
        if response.status_code != 303:
            print(f"FAILED Step 2: Status code {response.status_code}")
            sys.exit(1)
        
        redirect_url = response.headers.get("Location")
        print(f"SUCCESS: Project created. Redirecting to: {redirect_url}")
        # Extract project_id from redirect URL, e.g., /projects/vid_abc/view
        project_id = redirect_url.split("/")[2]
        print(f"Project ID: {project_id}")

    # 3. Test Packaging Optimization via LAVA
    print("\n[Step 3] Optimizing packaging and affiliate links via LAVA Video Optimizer...")
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(f"{base_url}/projects/{project_id}/optimize-packaging", follow_redirects=False)
        if response.status_code != 303:
            print(f"FAILED Step 3: Status code {response.status_code}")
            sys.exit(1)
        print("SUCCESS: Packaging optimized successfully.")

    # 4. Test Short-Video Splitting
    print("\n[Step 4] Splitting script into Shorts subprojects...")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{base_url}/projects/{project_id}/split", follow_redirects=False)
        if response.status_code != 303:
            print(f"FAILED Step 4: Status code {response.status_code}")
            sys.exit(1)
        print("SUCCESS: Script split into Shorts successfully.")

    # 5. Test Metrics Feedback Loop
    print("\n[Step 5] Feedbacking analytics metrics to the pipeline...")
    metrics_payload = {
        "ctr": 5.8,
        "avd": 145,
        "rpm": 12.40,
        "decision": "scale"
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{base_url}/projects/{project_id}/metrics", data=metrics_payload, follow_redirects=False)
        if response.status_code != 303:
            print(f"FAILED Step 5: Status code {response.status_code}")
            sys.exit(1)
        print("SUCCESS: Metrics registered and decision scale recorded.")
        
    print("\n=== ALL CLOSED LOOP PIPELINE STEPS COMPLETED SUCCESSFULY ===")


if __name__ == "__main__":
    asyncio.run(test_api_closed_loop())
