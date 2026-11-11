
CREATE DATABASE IF NOT EXISTS medislot
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE medislot;

-- 1. HOSPITALS
CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    hospital_name VARCHAR(200) NOT NULL,
    address_line VARCHAR(255) NOT NULL,
    city VARCHAR(100) NOT NULL,
    state VARCHAR(100) NOT NULL,
    postal_code VARCHAR(10) NOT NULL,
    contact_number VARCHAR(20) NULL,
    status ENUM('ACTIVE', 'DISABLED') NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (hospital_id),
    KEY idx_hospitals_city_status (city, status)
) ENGINE=InnoDB;

-- 2. SPECIALITIES
CREATE TABLE IF NOT EXISTS specialities (
    speciality_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    speciality_name VARCHAR(150) NOT NULL,
    description VARCHAR(500) NULL,
    status ENUM('ACTIVE', 'DISABLED') NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (speciality_id),
    UNIQUE KEY uq_specialities_name (speciality_name)
) ENGINE=InnoDB;

-- 3. DOCTORS
CREATE TABLE IF NOT EXISTS doctors (
    doctor_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    full_name VARCHAR(150) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    license_number VARCHAR(100) NOT NULL,
    hospital_id BIGINT UNSIGNED NOT NULL,
    qualifications VARCHAR(500) NULL,
    experience_years SMALLINT UNSIGNED NULL,
    bio TEXT NULL,
    profile_photo_key VARCHAR(500) NULL,
    status ENUM('ACTIVE', 'DISABLED') NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (doctor_id),
    UNIQUE KEY uq_doctors_email (email),
    UNIQUE KEY uq_doctors_license (license_number),
    KEY idx_doctors_hospital_status (hospital_id, status),

    CONSTRAINT chk_doctors_experience
        CHECK (experience_years IS NULL OR experience_years <= 80),

    CONSTRAINT fk_doctors_hospital
        FOREIGN KEY (hospital_id)
        REFERENCES hospitals (hospital_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 4. DOCTOR SPECIALITIES
CREATE TABLE IF NOT EXISTS doctor_specialities (
    doctor_id BIGINT UNSIGNED NOT NULL,
    speciality_id BIGINT UNSIGNED NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (doctor_id, speciality_id),
    KEY idx_doctor_specialities_speciality (speciality_id),

    CONSTRAINT fk_doctor_specialities_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,

    CONSTRAINT fk_doctor_specialities_speciality
        FOREIGN KEY (speciality_id)
        REFERENCES specialities (speciality_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 5. PATIENTS
CREATE TABLE IF NOT EXISTS patients (
    patient_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    full_name VARCHAR(150) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    date_of_birth DATE NOT NULL,
    phone_number VARCHAR(20) NOT NULL,
    gender ENUM(
        'MALE',
        'FEMALE',
        'NON_BINARY',
        'PREFER_NOT_TO_SAY'
    ) NOT NULL,
    status ENUM('ACTIVE', 'DISABLED') NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (patient_id),
    UNIQUE KEY uq_patients_email (email)
) ENGINE=InnoDB;

-- 6. CONSULTATION TYPE CATALOGUE
CREATE TABLE IF NOT EXISTS consultation_type_catalogue (
    catalogue_id TINYINT UNSIGNED NOT NULL,
    type_name VARCHAR(100) NOT NULL,
    duration_minutes SMALLINT UNSIGNED NOT NULL,
    min_capacity SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    max_capacity SMALLINT UNSIGNED NOT NULL DEFAULT 1,

    PRIMARY KEY (catalogue_id),
    UNIQUE KEY uq_consultation_catalogue_name (type_name),

    CONSTRAINT chk_catalogue_duration
        CHECK (duration_minutes > 0),

    CONSTRAINT chk_catalogue_capacity
        CHECK (
            min_capacity > 0
            AND max_capacity >= min_capacity
        )
) ENGINE=InnoDB;

INSERT INTO consultation_type_catalogue (
    catalogue_id,
    type_name,
    duration_minutes,
    min_capacity,
    max_capacity
)
VALUES
    (1, 'Quick Consult', 15, 1, 1),
    (2, 'Standard Consult', 30, 1, 1),
    (3, 'Detailed Consult', 60, 1, 1),
    (4, 'Health Camp Session', 60, 2, 10)
ON DUPLICATE KEY UPDATE
    type_name = VALUES(type_name),
    duration_minutes = VALUES(duration_minutes),
    min_capacity = VALUES(min_capacity),
    max_capacity = VALUES(max_capacity);

-- 7. DOCTOR CONSULTATION TYPES
CREATE TABLE IF NOT EXISTS consultation_types (
    consultation_type_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,
    catalogue_id TINYINT UNSIGNED NOT NULL,
    description VARCHAR(500) NULL,
    capacity SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    consultation_fee DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    status ENUM('ACTIVE', 'DISABLED') NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (consultation_type_id),

    UNIQUE KEY uq_consultation_type_doctor_id
        (doctor_id, consultation_type_id),

    UNIQUE KEY uq_consultation_types_doctor_catalogue
        (doctor_id, catalogue_id),

    KEY idx_consultation_types_doctor_status
        (doctor_id, status),

    CONSTRAINT chk_consultation_fee
        CHECK (consultation_fee >= 0),

    CONSTRAINT chk_consultation_capacity
        CHECK (
            (catalogue_id = 4 AND capacity BETWEEN 2 AND 10)
            OR
            (catalogue_id IN (1, 2, 3) AND capacity = 1)
        ),

    CONSTRAINT fk_consultation_types_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    CONSTRAINT fk_consultation_types_catalogue
        FOREIGN KEY (catalogue_id)
        REFERENCES consultation_type_catalogue (catalogue_id)
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 7. BLACKOUT DATES
CREATE TABLE IF NOT EXISTS blackout_dates (
    blackout_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,
    start_at DATETIME NOT NULL,
    end_at DATETIME NOT NULL,
    reason VARCHAR(500) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (blackout_id),
    KEY idx_blackout_doctor_time (doctor_id, start_at, end_at),

    CONSTRAINT chk_blackout_time
        CHECK (end_at > start_at),

    CONSTRAINT fk_blackout_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 8. DOCTOR AVAILABILITY PATTERNS
CREATE TABLE IF NOT EXISTS doctor_availability_patterns (
    pattern_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,
    consultation_type_id BIGINT UNSIGNED NOT NULL,
    day_of_week TINYINT UNSIGNED NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    status ENUM('ACTIVE', 'INACTIVE') NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (pattern_id),
    KEY idx_availability_doctor_day (
        doctor_id, day_of_week, status
    ),
    KEY idx_availability_consultation_type (
        consultation_type_id
    ),

    CONSTRAINT chk_availability_day
        CHECK (day_of_week BETWEEN 0 AND 6),

    CONSTRAINT chk_availability_time
        CHECK (end_time > start_time),

    CONSTRAINT fk_availability_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    CONSTRAINT fk_availability_consultation
        FOREIGN KEY (doctor_id, consultation_type_id)
        REFERENCES consultation_types (doctor_id, consultation_type_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 9. SLOTS
CREATE TABLE IF NOT EXISTS slots (
    slot_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    doctor_id BIGINT UNSIGNED NOT NULL,
    consultation_type_id BIGINT UNSIGNED NOT NULL,
    pattern_id BIGINT UNSIGNED NULL,
    start_at DATETIME NOT NULL,
    end_at DATETIME NOT NULL,
    capacity SMALLINT UNSIGNED NOT NULL DEFAULT 1,
    booked_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    status ENUM(
        'OPEN',
        'BLOCKED',
        'CANCELLED',
        'COMPLETED'
    ) NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (slot_id),
    UNIQUE KEY uq_slots_doctor_time (doctor_id, start_at, end_at),
    KEY idx_slots_doctor_start_status (doctor_id, start_at, status),
    KEY idx_slots_consultation_type (consultation_type_id),
    KEY idx_slots_pattern (pattern_id),

    CONSTRAINT chk_slots_time
        CHECK (end_at > start_at),

    CONSTRAINT chk_slots_capacity
        CHECK (
            capacity > 0
            AND booked_count <= capacity
        ),

    CONSTRAINT fk_slots_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctors (doctor_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    CONSTRAINT fk_slots_doctor_consultation
        FOREIGN KEY (doctor_id, consultation_type_id)
        REFERENCES consultation_types (doctor_id, consultation_type_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    CONSTRAINT fk_slots_availability_pattern
        FOREIGN KEY (pattern_id)
        REFERENCES doctor_availability_patterns (pattern_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 10. APPOINTMENTS
CREATE TABLE IF NOT EXISTS appointments (
    appointment_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    booking_reference VARCHAR(40) NOT NULL,
    patient_id BIGINT UNSIGNED NOT NULL,
    slot_id BIGINT UNSIGNED NOT NULL,
    status ENUM(
        'CONFIRMED',
        'COMPLETED',
        'CANCELLED_BY_PATIENT',
        'CANCELLED_BY_DOCTOR',
        'NO_SHOW'
    ) NOT NULL DEFAULT 'CONFIRMED',
    booked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cancelled_at TIMESTAMP NULL,
    cancellation_reason VARCHAR(500) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (appointment_id),
    UNIQUE KEY uq_appointment_patient_slot (patient_id, slot_id),
    UNIQUE KEY uq_appointments_reference (booking_reference),
    KEY idx_appointments_patient_status (patient_id, status),
    KEY idx_appointments_slot_status (slot_id, status),

    CONSTRAINT fk_appointments_patient
        FOREIGN KEY (patient_id)
        REFERENCES patients (patient_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    CONSTRAINT fk_appointments_slot
        FOREIGN KEY (slot_id)
        REFERENCES slots (slot_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 11. APPOINTMENT STATUS HISTORY
CREATE TABLE IF NOT EXISTS appointment_status_history (
    history_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    appointment_id BIGINT UNSIGNED NOT NULL,
    previous_status VARCHAR(30) NULL,
    new_status VARCHAR(30) NOT NULL,
    changed_by_role ENUM('PATIENT', 'DOCTOR', 'SYSTEM') NOT NULL,
    changed_by_id BIGINT UNSIGNED NULL,
    reason VARCHAR(500) NULL,
    changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (history_id),
    KEY idx_appointment_history (appointment_id, changed_at),

    CONSTRAINT fk_status_history_appointment
        FOREIGN KEY (appointment_id)
        REFERENCES appointments (appointment_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 12. APPOINTMENT RESCHEDULE HISTORY
CREATE TABLE IF NOT EXISTS appointment_reschedule_history (
    reschedule_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    appointment_id BIGINT UNSIGNED NOT NULL,
    previous_slot_id BIGINT UNSIGNED NOT NULL,
    new_slot_id BIGINT UNSIGNED NOT NULL,
    rescheduled_by_role ENUM('PATIENT', 'DOCTOR', 'SYSTEM') NOT NULL,
    rescheduled_by_id BIGINT UNSIGNED NULL,
    reschedule_number TINYINT UNSIGNED NOT NULL,
    reason VARCHAR(500) NULL,
    rescheduled_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (reschedule_id),
    UNIQUE KEY uq_appointment_reschedule_number
        (appointment_id, reschedule_number),
    KEY idx_reschedule_previous_slot (previous_slot_id),
    KEY idx_reschedule_new_slot (new_slot_id),

    CONSTRAINT chk_reschedule_number
        CHECK (reschedule_number BETWEEN 1 AND 2),

    CONSTRAINT fk_reschedule_appointment
        FOREIGN KEY (appointment_id)
        REFERENCES appointments (appointment_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    CONSTRAINT fk_reschedule_previous_slot
        FOREIGN KEY (previous_slot_id)
        REFERENCES slots (slot_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    CONSTRAINT fk_reschedule_new_slot
        FOREIGN KEY (new_slot_id)
        REFERENCES slots (slot_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 13. WAITLIST ENTRIES
CREATE TABLE IF NOT EXISTS waitlist_entries (
    waitlist_entry_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    slot_id BIGINT UNSIGNED NOT NULL,
    patient_id BIGINT UNSIGNED NOT NULL,
    status ENUM(
        'WAITING',
        'OFFERED',
        'BOOKED',
        'CANCELLED',
        'EXPIRED'
    ) NOT NULL DEFAULT 'WAITING',
    joined_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cancelled_at TIMESTAMP NULL,

    PRIMARY KEY (waitlist_entry_id),
    KEY idx_waitlist_slot_patient_status
        (slot_id, patient_id, status, joined_at),
    KEY idx_waitlist_queue (slot_id, status, joined_at),

    CONSTRAINT fk_waitlist_slot
        FOREIGN KEY (slot_id)
        REFERENCES slots (slot_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,

    CONSTRAINT fk_waitlist_patient
        FOREIGN KEY (patient_id)
        REFERENCES patients (patient_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 14. WAITLIST OFFERS
CREATE TABLE IF NOT EXISTS waitlist_offers (
    offer_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    waitlist_entry_id BIGINT UNSIGNED NOT NULL,
    status ENUM(
        'PENDING',
        'ACCEPTED',
        'REJECTED',
        'EXPIRED',
        'CANCELLED'
    ) NOT NULL DEFAULT 'PENDING',
    offered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    responded_at TIMESTAMP NULL,

    PRIMARY KEY (offer_id),
    KEY idx_waitlist_offer_expiry (status, expires_at),
    KEY idx_waitlist_offer_entry (waitlist_entry_id, offered_at),

    CONSTRAINT chk_waitlist_offer_expiry
        CHECK (expires_at > offered_at),

    CONSTRAINT fk_waitlist_offer_entry
        FOREIGN KEY (waitlist_entry_id)
        REFERENCES waitlist_entries (waitlist_entry_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 15. REFUND LEDGER
CREATE TABLE IF NOT EXISTS refund_ledger (
    refund_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    appointment_id BIGINT UNSIGNED NOT NULL,
    refund_reference VARCHAR(40) NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    reason VARCHAR(500) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (refund_id),
    UNIQUE KEY uq_refund_reference (refund_reference),
    UNIQUE KEY uq_refund_appointment (appointment_id),

    CONSTRAINT chk_refund_amount
        CHECK (amount > 0),

    CONSTRAINT fk_refund_appointment
        FOREIGN KEY (appointment_id)
        REFERENCES appointments (appointment_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- 16. OUTBOX EVENTS
CREATE TABLE IF NOT EXISTS outbox_events (
    event_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    event_uuid CHAR(36) NOT NULL,
    aggregate_type VARCHAR(80) NOT NULL,
    aggregate_id VARCHAR(100) NOT NULL,
    event_type VARCHAR(120) NOT NULL,
    payload JSON NOT NULL,
    status ENUM(
        'PENDING',
        'PROCESSING',
        'PUBLISHED',
        'FAILED'
    ) NOT NULL DEFAULT 'PENDING',
    attempts SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked_until TIMESTAMP NULL,
    published_at TIMESTAMP NULL,
    last_error TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (event_id),
    UNIQUE KEY uq_outbox_event_uuid (event_uuid),
    KEY idx_outbox_dispatch (status, next_attempt_at),
    KEY idx_outbox_aggregate (aggregate_type, aggregate_id)
) ENGINE=InnoDB;
