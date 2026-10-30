<div align="right">
	<a href="https://frappecloud.com/marketplace/apps/frappe_whatsapp" target="_blank">
		<picture>
			<source media="(prefers-color-scheme: dark)" srcset="https://frappe.io/files/try-on-fc-white.png">
			<img src="https://frappe.io/files/try-on-fc-black.png" alt="Try on Frappe Cloud" height="28" />
		</picture>
	</a>
</div>

# Frappe WhatsApp

[Documentation](https://shridarpatil.github.io/frappe_whatsapp/)

WhatsApp integration for Frappe/ERPNext. Use Meta's WhatsApp Cloud API directly without any third-party integration.

[![WhatsApp Video](https://img.youtube.com/vi/nq5Kcc5e1oc/0.jpg)](https://www.youtube.com/watch?v=nq5Kcc5e1oc)

[![YouTube](http://i.ytimg.com/vi/TncXQ0UW5UM/hqdefault.jpg)](https://www.youtube.com/watch?v=TncXQ0UW5UM)

![whatsapp](https://user-images.githubusercontent.com/11792643/203741234-29edeb1b-e2f9-4072-98c4-d73a84b48743.gif)

> **Note:** If you're not using live credentials, follow [step no 2](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started) to add the number on Meta to which you are sending messages.

## Features

- **Multi-Account Support** - Manage multiple WhatsApp Business accounts
- **Two-way Messaging** - Send and receive messages with full conversation tracking
- **Template Management** - Create and sync WhatsApp templates with Meta
- **WhatsApp Flows** - Build interactive forms using WhatsApp's native Flow Builder
- **Interactive Messages** - Send button and list messages for quick user responses
- **WhatsApp Notifications** - Automated notifications triggered by DocType events
- **Bulk Messaging** - Send campaigns to recipient lists with variable substitution
- **Webhook Support** - Real-time message delivery and status updates
- **Media Support** - Send and receive images, documents, videos, and audio files
- **Sync from Meta** - Import and sync templates and flows from your Meta Business Account
- **ERPNext Integration** - Native integration with Frappe/ERPNext DocTypes

## Installation

### Step 1: Get the app
```bash
bench get-app https://github.com/shridarpatil/frappe_whatsapp
```

### Step 2: Install on your site
```bash
bench --site [sitename] install-app frappe_whatsapp
```

## Quick Setup

### 1. Get WhatsApp Credentials
Visit [Meta Developer Portal](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started) to set up your WhatsApp Business API.

### 2. Configure WhatsApp Account
Go to **WhatsApp Account** in Frappe and enter your credentials:
- Account Name
- Access Token
- Phone Number ID
- Business Account ID
- App ID
- Webhook Verify Token

![WhatsApp Settings](https://user-images.githubusercontent.com/11792643/198827382-90283b36-f8ab-430e-a909-1b600d6f5da4.png)

### 3. Create Templates
Create WhatsApp templates that are approved by Meta:

![Create Template](https://user-images.githubusercontent.com/11792643/198827355-ebf9c113-f39a-4d37-98f7-38f719fb2d1f.png)

## Core Features

### WhatsApp Notifications

Automatically send WhatsApp messages based on DocType events.

**Supported Triggers:**
- DocType Events: Before/After Insert, Validate, Save, Submit, Cancel, Delete
- Scheduler Events: Hourly, Daily, Weekly, Monthly
- Date-based: Days Before/After a specified date field

![WhatsApp Notification](https://user-images.githubusercontent.com/11792643/198827295-f6d756a3-6289-40b3-99ea-0394efb61041.png)

**Features:**
- Map template parameters to DocType fields
- Add conditions using Python expressions
- Attach document print PDFs or custom files
- Set DocType field values after sending (e.g., mark as notified)
- Support for interactive buttons with dynamic URLs

### Bulk WhatsApp Messages

Send WhatsApp messages to multiple recipients at once.

**Features:**
- Import recipients from any DocType (Customer, Contact, etc.)
- Create recipient lists for reuse
- Variable substitution from recipient data
- Background processing with progress tracking
- Retry failed messages
- Select specific WhatsApp account for sending

**Recipient Types:**
1. **Individual Recipients** - Add recipients directly with their phone numbers
2. **Recipient List** - Use a pre-configured WhatsApp Recipient List

**Variable Types:**
1. **Common** - Same values for all recipients
2. **Unique** - Different values per recipient (from recipient data)

### Direct Messaging

Send messages without templates (within 24-hour window):

![Direct Message](https://user-images.githubusercontent.com/11792643/211518862-de2d3fbc-69c8-48e1-b000-8eebf20b75ab.png)

### Interactive Messages

Send interactive button and list messages for quick user responses.

**Button Messages:**
- Up to 3 quick reply buttons
- Users tap to respond instantly
- Great for confirmations, options, and CTAs

**List Messages:**
- Up to 10 items organized in sections
- Users select from a menu
- Ideal for product catalogs, service menus, FAQs

### WhatsApp Flows

Build interactive forms using WhatsApp's native Flow Builder. Flows provide a rich UI experience for collecting structured data from users.

**Features:**
- Visual form builder with multiple screens
- Support for text inputs, dropdowns, date pickers, and more
- Conditional branches using ordered route conditions and a default route
- Structured authoring or direct editing of advanced Meta Flow JSON
- Per-phone-number Flow availability within a WhatsApp Business Account
- Client-only flows (no endpoint required)
- Publish and manage flows directly from Frappe
- Send flow messages and receive responses via webhook
- Launch with `navigate` (default) or `data_exchange`, including an initial screen and JSON data
- Separate Flow JSON schema and Data Exchange protocol versions

**Supported Field Types:**
- TextInput, TextArea
- Dropdown (with static options)
- DatePicker
- RadioButtonsGroup, CheckboxGroup
- OptIn
- Text display components (TextHeading, TextBody, TextSubheading, TextCaption)

**Flow Actions:**
- Create on WhatsApp - Push new flows to Meta
- Upload Flow JSON - Update flow definition
- Publish - Make flow available to all users
- Deprecate - Mark flow as deprecated
- Send Test - Test draft flows with registered test numbers
- Sync from Meta - Import existing flows back to Frappe

**Branches and availability:**
- Add rows under **Screen Routes** to route a screen conditionally. Use Meta expressions such as `${form.selection} == 'option_a'` and add one default route per branching screen.
- **Authoring Mode** only selects the writable editor. Every save detects whether JSON or the structured tables changed and synchronizes the other representation; JSON wins conflicts. Screens, Form-contained fields, Footer actions, client-side branches, server-driven routes, and per-screen data schemas round-trip losslessly, while unsupported Meta constructs are rejected rather than discarded.
- Set **Available On** to all phone numbers in the Flow's WABA or select an explicit set of WhatsApp Accounts/phone numbers. The same restriction is enforced when sending.
- On a Flow message, use **Flow Action**, **Flow Screen**, and **Initial Flow Data** for dynamic launches. **Flow Action Payload** accepts the complete Meta payload when additional keys are needed.

**Encrypted Data Exchange:**

1. In **WhatsApp Account**, use **Flows > Stage New Flow Encryption Key**, then **Register Pending Flow Key** and **Verify and Activate Pending Key**. Generation never replaces the active key. Verification requires both Meta's `VALID` signature status and an exact cryptographic match before the pending key is promoted; the former active private key is retained for fallback decryption. Add the Meta App Secret; request-signature enforcement defaults on and cannot be disabled outside developer mode. Private keys, passphrases, and the app secret are protected Password fields and are never written to protocol logs.
2. Set the Flow's **Backend Handler Key**. The app generates a Flow-specific **Data Endpoint URI** using the current site URL and a URL-encoded `flow` hint. You may supply a different base URI; the Flow hint is added or replaced automatically. **Create on WhatsApp**, **Update Flow Metadata**, and **Upload Flow JSON** register the resulting URI with Meta.
3. Map the same handler key to a trusted method in an installed app's `hooks.py`:

```python
whatsapp_flow_handlers = {
    "example_registration": "my_app.api.handle_whatsapp_flow",
}
```

Only the one method mapped to the Flow's key receives its normalized, decrypted data. Missing keys and keys claimed by multiple apps fail closed. Flow records never contain executable Python paths. The handler returns an ordinary response; `frappe_whatsapp` owns Meta's RSA-OAEP/AES-GCM envelope protocol in both directions:

```python
def handle_whatsapp_flow(context):
    if context["flow"] != "Example Registration":
        return None

    # context contains flow, whatsapp_account, action, screen,
    # flow_token, and data.
    return {
        "screen": "SUMMARY",
        "data": {"request_type": context["data"].get("request_type")},
    }
```

Completed Flow answers are stored structurally in `WhatsApp Message.flow_response`; the message summary is neutral and realtime notifications contain only the incoming document name. `WhatsApp Notification Log` remains the intentional raw webhook receipt and should be governed by normal ERP permissions and retention rules.

For `INIT`, `data_exchange`, and `BACK`, the encrypted `flow_token` must match a persisted outgoing Flow message on the identified WhatsApp Account. The Flow-specific URL hint is used only for encrypted `ping` health checks.

### Scripted conversations

**WhatsApp Conversation Script** provides local, stateful message journeys without publishing an artifact to Meta. Define an entry trigger, ordered steps, and reply transitions using Structured tables or synchronized JSON. Supported steps can send text, buttons/lists, launch a WhatsApp Flow, invoke an allowlisted installed-app action, hand off, or finish.

Each active customer journey is persisted as a **WhatsApp Conversation Session**, scoped by WhatsApp Account and phone number. Incoming messages advance the current step after the webhook transaction commits. Exact, contains, regex, content-type, any, and default transitions are supported, with optional context capture and `{{ variable }}` substitution in outgoing text.

Conversation sends use a durable **WhatsApp Conversation Outbox**. The outgoing `WhatsApp Message`, unique incoming-message/step execution key, session transition, and processed marker commit before a separate worker contacts Meta. Delivery is durably claimed as `Sending` first. If the worker cannot prove whether Meta accepted the request, the entry becomes `Unknown` and is never retried automatically; a System Manager may explicitly retry after accepting the duplicate-delivery risk.

Custom actions use declarative hook keys rather than Server Scripts:

```python
whatsapp_conversation_handlers = {
    "update_record": "my_app.api.update_record",
}
```

The handler receives script, session, account, phone number, current step, incoming-message identifier, and context. It may return context updates, a next step, or a terminal session status.

### Sync from Meta

Keep your Frappe data synchronized with your Meta Business Account.

**Templates:**
- Go to **WhatsApp Templates** list view
- Click **Sync from Meta** button
- All templates from active WhatsApp accounts are imported/updated

**Flows:**
- Go to **WhatsApp Flow** list view
- Click **Sync from Meta** button
- Select the WhatsApp Account to sync from
- New flows are imported, existing flows are updated with latest status and JSON

### Custom Data Templates

Send templates using custom data instead of DocType fields:

```python
doc.set("_data_list", [
    {"phone": "+1234567890", "name": "John", "order_id": "ORD-001"},
    {"phone": "+0987654321", "name": "Jane", "order_id": "ORD-002"}
])
```

![Custom Data](https://github.com/user-attachments/assets/7496b081-df2b-41dc-bdcb-ed7e5f464698)

## Webhook Setup

### Configure Webhook on Meta
1. Go to your Meta Developer App
2. Set Webhook URL: `<your-domain>/api/method/frappe_whatsapp.utils.webhook.webhook`
3. Add Verify Token (same as in WhatsApp Account settings)
4. Subscribe to webhook fields:
   - `messages` - to receive incoming messages
   - `message_template_status_update` - for template status updates

### Incoming Messages
Messages received via webhook are automatically created as WhatsApp Message documents:

![Incoming Message](https://user-images.githubusercontent.com/11792643/211519625-a528abe2-ba24-46a4-bcbc-170f6b4e27fb.png)

## Multi-Account Support

Manage multiple WhatsApp Business accounts for different use cases:

- Set default accounts for incoming and outgoing messages
- Select specific accounts when sending bulk messages
- Route notifications through designated accounts
- Auto-read receipts per account

## Recommended Apps

Enhance your WhatsApp experience with these companion apps:

### WhatsApp Chat
A chat app for Frappe Desk to manage WhatsApp conversations.

| | |
|---|---|
| **Features** | Real-time messaging, media support, contact management, read receipts |
| **Install** | `bench get-app https://github.com/shridarpatil/whatsapp_chat` |
| **Marketplace** | <a href="https://frappecloud.com/marketplace/apps/whatsapp_chat"><img src="https://frappe.io/files/try-on-fc-black.png" alt="Try on Frappe Cloud" height="28" /></a> |
| **Use Cases** | Customer conversations, sales follow-ups, support tickets |

### WhatsApp Chatbot
Build automated chatbots with flows, keyword replies, and AI-powered responses.

| | |
|---|---|
| **Features** | Multi-step flows, keyword matching, AI fallback (OpenAI/Anthropic/Google), session management |
| **Install** | `bench get-app https://github.com/shridarpatil/frappe_whatsapp_chatbot` |
| **Marketplace** | <a href="https://frappecloud.com/marketplace/apps/frappe_whatsapp_chatbot"><img src="https://frappe.io/files/try-on-fc-black.png" alt="Try on Frappe Cloud" height="28" /></a> |
| **Use Cases** | Customer support, order status, appointment booking, lead capture |

## Documentation

For detailed documentation, visit [https://shridarpatil.github.io/frappe_whatsapp/](https://shridarpatil.github.io/frappe_whatsapp/)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT
