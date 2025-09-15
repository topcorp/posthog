# Liquid Templating in PostHog CDP

Liquid is a templating language that allows you to customize your destination payloads with dynamic data from PostHog events and person properties.

## Basic Syntax

### Variables
Use double curly braces to output variables:
```liquid
{{ person.name }}
{{ event.properties.page_title }}
```

### Filters
Apply filters to transform data:
```liquid
{{ person.name | upcase }}
{{ event.timestamp | date: "%Y-%m-%d" }}
{{ event.properties.price | round: 2 }}
```

## Available Data in PostHog

### Event Data
- `{{ event.event }}` - Event name
- `{{ event.timestamp }}` - Event timestamp
- `{{ event.properties }}` - Event properties object
- `{{ event.properties.property_name }}` - Specific event property

### Person Data
- `{{ person.properties }}` - Person properties object
- `{{ person.properties.email }}` - Person email
- `{{ person.properties.name }}` - Person name

### Common Filters
- `{{ value | upcase }}` - Convert to uppercase
- `{{ value | downcase }}` - Convert to lowercase  
- `{{ value | capitalize }}` - Capitalize first letter
- `{{ value | strip }}` - Remove whitespace
- `{{ value | truncate: 50 }}` - Truncate to 50 characters
- `{{ value | default: "fallback" }}` - Use fallback if value is empty

## Control Flow

### Conditionals
```liquid
{% if person.properties.email %}
  "email": "{{ person.properties.email }}"
{% endif %}
```

### Loops
```liquid
{% for property in event.properties %}
  "{{ property[0] }}": "{{ property[1] }}"
{% endfor %}
```

## Best Practices

1. **Always check for data existence** before using it:
   ```liquid
   {% if person.properties.email %}
     {{ person.properties.email }}
   {% else %}
     "unknown@example.com"
   {% endif %}
   ```

2. **Use appropriate filters** for data formatting:
   ```liquid
   {{ event.timestamp | date: "%Y-%m-%d %H:%M:%S" }}
   ```

3. **Handle arrays and objects safely**:
   ```liquid
   {% if event.properties.items.size > 0 %}
     {{ event.properties.items | first }}
   {% endif %}
   ```

## Examples

### Basic Event Payload
```liquid
{
  "event_name": "{{ event.event }}",
  "timestamp": "{{ event.timestamp }}",
  "user_email": "{{ person.properties.email | default: 'unknown' }}"
}
```

### Conditional Properties
```liquid
{
  "event": "{{ event.event }}"
  {% if person.properties.email %},
  "email": "{{ person.properties.email }}"
  {% endif %}
  {% if event.properties.revenue %},
  "revenue": {{ event.properties.revenue }}
  {% endif %}
}
```

## Advanced Features

### Date and Time Formatting
```liquid
{{ event.timestamp | date: "%Y-%m-%d" }}          // 2024-01-15
{{ event.timestamp | date: "%B %d, %Y" }}         // January 15, 2024
{{ event.timestamp | date: "%H:%M:%S" }}          // 14:30:45
{{ event.timestamp | date: "%Y-%m-%dT%H:%M:%SZ" }} // ISO format
```

### String Manipulation
```liquid
{{ person.properties.name | slice: 0, 5 }}        // First 5 characters
{{ event.event | replace: "_", " " | capitalize }} // Replace underscores with spaces
{{ person.properties.email | split: "@" | first }} // Get username from email
```

### Math Operations
```liquid
{{ event.properties.price | times: 1.08 | round: 2 }} // Add 8% tax
{{ event.properties.quantity | plus: 1 }}             // Add 1 to quantity  
{{ event.properties.score | divided_by: 100.0 }}      // Convert to percentage
```

### Array and Object Handling
```liquid
{{ event.properties.tags | join: ", " }}              // Join array elements
{{ event.properties.items | size }}                   // Get array length
{{ event.properties.metadata | keys | join: ";" }}    // Get object keys
```

## PostHog-Specific Data Access

### Event Context
```liquid
{{ event.uuid }}           // Event unique identifier
{{ event.distinct_id }}    // User distinct ID
{{ event.team_id }}        // Team/project ID
{{ event.elements_chain }} // Element interaction chain (autocapture)
```

### Person Properties Deep Access
```liquid
{{ person.properties['$initial_utm_source'] }}    // UTM data
{{ person.properties['$browser'] }}               // Browser info
{{ person.properties['$os'] }}                    // Operating system
{{ person.properties['$device_type'] }}           // Device type
```

### Safe Property Access
```liquid
{% assign email = person.properties.email | default: person.properties['$email'] %}
{% if email %}
  "contact_email": "{{ email }}"
{% else %}
  "contact_email": null
{% endif %}
```

## Common Patterns

### Webhook Payload Construction
```liquid
{
  "event_type": "{{ event.event }}",
  "user_id": "{{ event.distinct_id }}",
  "timestamp": "{{ event.timestamp | date: '%Y-%m-%dT%H:%M:%SZ' }}",
  {% if person.properties.email %}
  "user_email": "{{ person.properties.email }}",
  {% endif %}
  "properties": {
    {% for property in event.properties %}
    "{{ property[0] }}": {% if property[1] == null %}null{% elsif property[1] == true %}true{% elsif property[1] == false %}false{% elsif property[1] contains '.' and property[1] == property[1] | plus: 0 %}{{ property[1] }}{% else %}"{{ property[1] }}"{% endif %}{% unless forloop.last %},{% endunless %}
    {% endfor %}
  }
}
```

### Email Template
```liquid
Subject: {{ event.event | replace: "_", " " | capitalize }}

Hello {% if person.properties.name %}{{ person.properties.name }}{% else %}there{% endif %},

{% if event.event == 'user_signed_up' %}
Welcome to our platform! 🎉
{% elsif event.event == 'purchase_completed' %}
Thank you for your purchase of ${{ event.properties.total | default: 0 }}.
{% else %}
We noticed you {{ event.event | replace: "_", " " }}.
{% endif %}

{% if event.properties.page_url %}
This happened on: {{ event.properties.page_url }}
{% endif %}

Best regards,
The Team
```

## Troubleshooting

### Common Issues

1. **Null/Undefined Properties**
   ```liquid
   {% comment %} Wrong - will break if property doesn't exist {% endcomment %}
   {{ event.properties.missing_property }}
   
   {% comment %} Correct - safe with fallback {% endcomment %}
   {{ event.properties.missing_property | default: "N/A" }}
   ```

2. **Type Mismatches**
   ```liquid
   {% comment %} Wrong - treating number as string {% endcomment %}
   {{ event.properties.count | upcase }}
   
   {% comment %} Correct - check type first {% endcomment %}
   {% if event.properties.count %}{{ event.properties.count }}{% endif %}
   ```

3. **JSON Escaping**
   ```liquid
   {% comment %} Wrong - unescaped quotes can break JSON {% endcomment %}
   "message": "{{ person.properties.notes }}"
   
   {% comment %} Correct - escape quotes and newlines {% endcomment %}
   "message": "{{ person.properties.notes | replace: '"', '\"' | replace: '\n', '\\n' }}"
   ```

### Performance Tips

- Use conditionals to avoid processing unnecessary data
- Cache repeated calculations in variables using `{% assign %}`
- Prefer specific property access over loops when possible
- Use `default` filter instead of complex conditionals for fallbacks

## Debugging Templates

### Testing Data Access
```liquid
{% comment %} Debug output - remove before production {% endcomment %}
DEBUG - Event: {{ event.event }}
DEBUG - Properties: {{ event.properties | json }}
DEBUG - Person: {{ person.properties | json }}

{% comment %} Your actual template below {% endcomment %}
```

### Validation Checks
```liquid
{% comment %} Validate required data exists {% endcomment %}
{% unless event.event %}
  {% error "Event name is required" %}
{% endunless %}

{% unless person.properties.email %}
  {% comment %} Log warning but continue {% endcomment %}
  {% assign warning = "No email found for user" %}
{% endunless %}
```

## External Reference

For complete Liquid syntax reference and advanced features, see the [official Liquid documentation](https://liquidjs.com/filters/overview.html).