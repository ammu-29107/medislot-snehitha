USE medislot;

-- Initial hospital
INSERT INTO hospitals (
    hospital_name,
    address_line,
    city,
    state,
    postal_code,
    contact_number
)
SELECT
    'MediSlot General Hospital',
    'Madhapur',
    'Hyderabad',
    'Telangana',
    '500081',
    '+914012345678'
WHERE NOT EXISTS (
    SELECT 1
    FROM hospitals
    WHERE hospital_name = 'MediSlot General Hospital'
      AND city = 'Hyderabad'
);

-- Initial specialities
INSERT INTO specialities (speciality_name, description)
VALUES
    ('Cardiology', 'Diagnosis and treatment of heart conditions'),
    ('Dermatology', 'Diagnosis and treatment of skin conditions'),
    ('General Medicine', 'Primary and general medical care'),
    ('Neurology', 'Diagnosis and treatment of nervous system disorders'),
    ('Orthopedics', 'Diagnosis and treatment of musculoskeletal conditions'),
    ('Pediatrics', 'Medical care for infants, children, and adolescents'),
    ('Gynecology', 'Healthcare related to the female reproductive system')
AS new
ON DUPLICATE KEY UPDATE
    description = new.description;