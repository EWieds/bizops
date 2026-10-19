# Getting started with bizops

Total time: about 2 hours, most of it creating accounts. You do this once.

## Part 1: Create accounts (in your name)

These are tied to your identity by law; agents can't create them for you.

1. **Fly.io** (hosting, ~$15-25/month): fly.io -> Sign up -> add a card.
2. **A domain** (~$12/year): Cloudflare or Namecheap, e.g. `yourbrand.com`.
3. **A mailbox on that domain** (~$7/month): Google Workspace -> create `hello@yourbrand.com`.
   1. In that Google account: Security -> turn on **2-Step Verification**.
   2. Go to https://myaccount.google.com/apppasswords -> create an app password -> copy it.
   3. Google Workspace Admin -> Apps -> Gmail -> **Authenticate email (DKIM)** -> turn it on.
   4. At your domain provider, add the **SPF** and **DMARC** records Google shows you. This keeps your
      emails out of spam.
4. **A postal address** (required by anti-spam law): a PO box or virtual mailbox.
5. **Stripe** (stripe.com): sign up, verify your identity, add your bank account. Then:
   1. Developers -> API keys -> **Create restricted key**.
   2. Allow **Write** on Products, Prices and Payment Links.
   3. Allow **Read** on Checkout Sessions and Events.
   4. Copy the key (starts with `rk_live_`).
6. **Anthropic API key** (console.anthropic.com -> API keys): how the always-on server reaches Claude.
   Set a monthly spend limit there too. (Running a subscription login on a 24/7 server may be against
   Anthropic's terms, so the deploy script asks for an API key.)
7. **Privacy.com** (privacy.com, free tier): virtual cards so agents can buy tools and subscriptions
   themselves. Sign up, verify your identity, link a bank account, then Account -> API -> copy the key.
   Each purchase your Chief Investment Officer approves gets its own card, locked to that vendor, for
   exactly the approved amount, and agents can never spend more than you load into a business's wallet.
8. **Optional:**
   - **Jev / TypeSafe key** (typesafe.ai): cheaper, faster decisions.
   - **Vapi key** (vapi.ai): only if a business sells real phone agents.

## Part 2: Get the code

Open Terminal (Mac: Cmd+Space, type "Terminal") and run:
```
git clone https://github.com/EWieds/bizops.git
```
(The deploy script installs the Fly.io command-line tool itself if it's missing.)

## Part 3: Put bizops on the internet (~10 minutes)

In Terminal:
```
cd bizops
FLY_ORG=evan-wieds bash scripts/deploy-fly.sh
```
- A browser opens to log in to Fly.io. Approve it.
- Paste your Anthropic API key when asked (the input is hidden).
- At the end it prints your **dashboard address** (like `https://bizops-you-1234.fly.dev`) and your
  **password**. Save both somewhere safe.

If anything errors, copy the error text and send it to Claude to fix.

## Part 4: Connect your accounts (once)

Open your dashboard address, log in, click **Connect accounts**, and press **Test and save** on each:

1. **Stripe**: paste the `rk_live_...` key.
2. **Email**:
   - SMTP host `smtp.gmail.com`, port `587`; IMAP host `imap.gmail.com`, port `993`
   - Username: your mailbox (`hello@yourbrand.com`); password: the app password
   - From address: `hello@yourbrand.com`
   - Legal sender name: your name or company
   - Postal address: your PO box
3. **Virtual cards**: paste your Privacy.com API key, the name on the card and your billing ZIP.
   Then on **Money**, load each business's wallet with what its agents may spend.
4. **Model access** (optional): paste your Jev key, or replace the Anthropic API key.
5. **Password** (optional): change it to one you'll remember.

## Part 5: Start a business

Pick one:
- **Let it find one:** go to **Ideas** -> **Research new ideas now**. Proposals appear within the hour.
  Strong ones are launched automatically (you'll get a chat message), or press **Launch now** yourself.
- **Give it your idea:** **New business** (or Chat -> Platform CEO -> **Launch as a business**). The analyst writes
  the full business case and an independent red team attacks it, hardest of all because it's yours. You get
  the verdict and the main objections in chat; if it says no, you can still launch it from **Ideas**.
- **From a video or document:** in **Chat**, paste a YouTube link or attach a PDF/Word file, then press
  **Business plan**, **Build a website** or **Launch as a business**.

## Part 6: Supervise (10 minutes a day, the first week)

Security is on by default: 5 wrong passwords lock that address out for 15 minutes, and **System ->
Login sessions** shows every logged-in browser with a revoke button. Changing the password logs out
everything.

- **Needs you**: the only things that require you (signing contracts, approving a new vendor, etc.).
- **Sales & customers**: read the first emails the agents send; see leads, replies, payments, customers.
- **Activity**: everything every agent did, with its reason and cost.
- **Money**: spend vs revenue per business, refunds and chargebacks (subtracted automatically), and the
  monthly reinvestment waterfall (verified profit -> tax reserve -> operating reserve -> reinvest/treasury).
- **Chargebacks** appear in Needs you: respond to them in the Stripe dashboard.
- **Audit**: issues the outside auditor found.
- **Emergency**: the red **Kill switch** in the sidebar stops everything instantly.

The CEOs review their teams' actual work every 12 hours and send weak work back to the right specialist,
so quality improves on its own; your job is spot-checks and the Needs-you queue.
