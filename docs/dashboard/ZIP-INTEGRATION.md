# Milestone 5 Dashboard ZIP integration


This ZIP contains the full project snapshot plus Milestone 5 appointment dashboard integration.


## Dashboard changes
- Patient: booking controls on doctor profile availability cards, patient appointment list, appointment detail view, and cancellation.
- Doctor: appointment management page with patient name, phone, and email; appointment cancellation.
- Schedule: slot cancellation action, including the backend's affected-appointments handling.
- Appointment API: doctor appointment list now includes a nested `patient` object with name and contact fields; patient-facing list responses omit these contact fields.
- Postman: collection and environment JSON files are in `postman/`.
- Automated route tests: `tests/test_dashboard_appointments.py`.


## Apply
Extract the ZIP into a separate folder first, then copy/merge its contents into your project folder. Review the changes before staging or deploying. No commit, push, or deployment is included.


## Verify locally
PowerShell from the project root:
```powershell
python -m compileall dashboard src/appointments/api tests
pytest -q
```

Dashboard environment variables required to start the app:
```powershell
$env:FLASK_SECRET_KEY = "<local-secret>"
$env:MEDISLOT_API_BASE_URL = "<deployed-api-base-url>"
python -m flask --app dashboard.app run
```

For Postman, import the appointment collection and environment. Set the API base URL and access tokens, then supply a valid slot ID for booking/cancel-slot and an appointment ID for appointment detail/cancellation.
