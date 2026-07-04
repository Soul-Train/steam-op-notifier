Steam Overwhelmingly Positive Notifier
Emails you when any game on Steam newly reaches Overwhelmingly Positive. Each game is reported once, ever. Dips and recoveries are ignored.
Setup (about 10 minutes)
Create the repo. On github.com, make a new private repo (e.g. `steam-op-notifier`) and upload these three files, keeping the folder structure: `steam_op_notifier.py`, `README.md`, and `.github/workflows/daily.yml`.
Create a Gmail app password. Go to myaccount.google.com > Security > 2-Step Verification > App passwords. Create one named "steam notifier" and copy the 16-character password. (Regular Gmail password will not work.)
Add three repo secrets. In the repo: Settings > Secrets and variables > Actions > New repository secret:
`EMAIL_USER` = your Gmail address
`EMAIL_PASS` = the app password from step 2
`EMAIL_TO` = where you want the notifications (can be the same address)
Run it once manually. Repo > Actions tab > "Steam OP Daily Check" > Run workflow. This seeds the ~500 currently-OP games and sends a one-time confirmation email.
Done. It runs daily at 9 AM Eastern. You only get email when something new crosses the threshold.
Notes
State lives in `seen.json`, committed back to the repo after each run.
To change the schedule, edit the cron line in `.github/workflows/daily.yml`.
If Steam changes their search page layout, the run fails loudly instead of silently missing games. GitHub emails you on failed workflow runs by default.
