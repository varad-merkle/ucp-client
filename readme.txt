┌───────────────────┬──────────────────┐
│       Field       │      Value       │
├───────────────────┼──────────────────┤
│ Email             │ test@example.com │
├───────────────────┼──────────────────┤
│ First name        │ Test             │
├───────────────────┼──────────────────┤
│ Last name         │ Buyer            │
├───────────────────┼──────────────────┤
│ Phone             │ +1 217 555 0123  │
├───────────────────┼──────────────────┤
│ Street address    │ 123 Main St      │
├───────────────────┼──────────────────┤
│ City              │ Springfield      │
├───────────────────┼──────────────────┤
│ State / region    │ IL               │
├───────────────────┼──────────────────┤
│ Postal / ZIP code │ 62701            │
├───────────────────┼──────────────────┤
│ Country code      │ US               │





pip install pysonar

pysonar \
  --sonar-host-url=http://localhost:9000 \
  --sonar-token=sqp_8e2de837f9e266b9564cb3a798cba3b7ba5e8276 \
  --sonar-project-key=UCP_MCP


  mvn clean verify org.sonarsource.scanner.maven:sonar-maven-plugin:sonar \
  -Dsonar.projectKey=UCP_MCP \
  -Dsonar.projectName='UCP_MCP' \
  -Dsonar.host.url=http://localhost:9000 \
  -Dsonar.token=sqp_8e2de837f9e266b9564cb3a798cba3b7ba5e8276



  1. Start SonarQube (skip this if it's already running)

Open a normal PowerShell window (not "Run as administrator") and run:

cd C:\Users\owankh01\Downloads\sonarqube-26.9.0.129388\sonarqube-26.9.0.129388\bin\windows-x86-64
.\StartSonar.bat

Wait until it prints SonarQube is operational, then keep this window open. Closing it stops the server.

To check whether it's already running, open http://localhost:9000. If the page loads, skip this step.

2. Run the scan

Open a second terminal and run:
cd C:\Users\owankh01\POC-5.10_FE_Catalog\ucp-client
pysonar --sonar-host-url=http://localhost:9000 --sonar-token=<your-token> --sonar-project-key=UCP_MCP
Keep it on one line, with no \.

Wait for ANALYSIS SUCCESSFUL.

3. View the results

http://localhost:9000/dashboard?id=UCP_MCP

When you're done

To stop SonarQube, press Ctrl+C in the window from step 1.

Optional: to avoid typing the token every time, save it once as a user environment variable:
setx SONAR_TOKEN <your-token>
After you open a new terminal, the scan command becomes:
pysonar --sonar-host-url=http://localhost:9000 --sonar-project-key=UCP_MCP